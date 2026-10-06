import logging
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from curl_cffi import requests as cffi_requests

from .config import settings
from .db import (
    add_restock_log,
    list_enabled_webhooks,
    load_event_state,
    upsert_event_state,
)
from .security import decrypt_webhook


# ============================================================
# LOGGING
# ============================================================

log = logging.getLogger("48group.monitor")


# ============================================================
# EVENT CONFIG
# ============================================================

EVENTS = [
    {
        "group": "JKT48",
        "api_url": (
            "https://jkt48.com/api/v1/exclusives/"
            "EX5B99/bonus?lang=id"
        ),
        "buy_url": (
            "https://jkt48.com/purchase/"
            "exclusive?code=EX5B99"
        ),
    },
    {
        "group": "AKB48",
        "api_url": (
            "https://jkt48.com/api/v1/exclusives/"
            "EXD1A1/bonus?lang=id"
        ),
        "buy_url": (
            "https://jkt48.com/purchase/"
            "exclusive?code=EXD1A1"
        ),
    },
]


EVENT_MAP = {
    event["group"]: event
    for event in EVENTS
}


# ============================================================
# DISCORD COLORS
# ============================================================

COLOR_GREEN = 0x2ECC71
COLOR_RED = 0xE74C3C
COLOR_BLUE = 0x3498DB
COLOR_PURPLE = 0x9B59B6


# ============================================================
# RUNTIME STATE
# ============================================================

STATE_LOCK = threading.Lock()

RUNTIME_STATE = {
    "members": [],
    "last_check": None,
    "last_success": None,
    "last_error": None,
    "running": False,
}


# ============================================================
# FETCH API
# ============================================================

def fetch_api(url: str):
    """
    Hanya melakukan SATU request per endpoint.

    Ini penting supaya ketika server memberikan 403/429,
    monitor tidak memperparah rate-limit / anti-bot protection.

    Return:
        data
        error
        status_code
        retry_after
    """

    try:

        log.info(
            "GET %s",
            url,
        )

        response = cffi_requests.get(
            url,
            impersonate="chrome",
            timeout=20,
        )


        status_code = response.status_code


        log.info(
            "API HTTP %s | %s",
            status_code,
            url,
        )


        # ====================================================
        # SUCCESS
        # ====================================================

        if status_code == 200:

            try:

                data = response.json()

                return (
                    data,
                    None,
                    200,
                    None,
                )

            except Exception as exc:

                return (
                    None,
                    (
                        "Response HTTP 200 "
                        "tetapi JSON tidak valid: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                    200,
                    None,
                )


        # ====================================================
        # RATE LIMIT
        # ====================================================

        if status_code == 429:

            retry_after = 120

            try:

                header_retry = (
                    response.headers.get(
                        "Retry-After"
                    )
                )

                if header_retry:

                    retry_after = max(
                        60,
                        int(float(header_retry)),
                    )

            except Exception:
                pass


            return (
                None,
                (
                    "HTTP 429 - server melakukan "
                    "rate limit"
                ),
                429,
                retry_after,
            )


        # ====================================================
        # CLOUDFLARE / FORBIDDEN
        # ====================================================

        if status_code == 403:

            log.warning(
                "HTTP 403 anti-bot/protection "
                "terdeteksi untuk %s",
                url,
            )

            return (
                None,
                (
                    "HTTP 403 - endpoint sementara "
                    "menolak request otomatis"
                ),
                403,
                300,
            )


        # ====================================================
        # OTHER HTTP ERROR
        # ====================================================

        preview = ""

        try:

            preview = (
                response.text[:120]
                .replace("\n", " ")
                .replace("\r", " ")
            )

        except Exception:
            pass


        return (
            None,
            (
                f"HTTP {status_code}"
                + (
                    f" - {preview}"
                    if preview
                    else ""
                )
            ),
            status_code,
            60,
        )


    except Exception as exc:

        log.warning(
            "API exception %s: %s",
            type(exc).__name__,
            exc,
        )

        return (
            None,
            (
                f"{type(exc).__name__}: "
                f"{exc}"
            ),
            None,
            30,
        )


# ============================================================
# PARSE API
# ============================================================

def parse_api_data(
    response_json,
    group_name,
    buy_url,
):

    parsed_items = []


    if not isinstance(
        response_json,
        dict,
    ):

        return parsed_items


    sessions = response_json.get(
        "data",
        [],
    )


    if not isinstance(
        sessions,
        list,
    ):

        return parsed_items


    for session_obj in sessions:

        if not isinstance(
            session_obj,
            dict,
        ):

            continue


        session_name = session_obj.get(
            "label",
            "-",
        )


        session_members = session_obj.get(
            "session_members",
            [],
        )


        if not isinstance(
            session_members,
            list,
        ):

            continue


        for detail in session_members:

            if not isinstance(
                detail,
                dict,
            ):

                continue


            member_name = detail.get(
                "member_name",
                "Unknown",
            )


            track = detail.get(
                "label",
                "-",
            )


            try:

                stock = int(
                    detail.get(
                        "available_quota",
                        0,
                    )
                    or 0
                )

            except (
                TypeError,
                ValueError,
            ):

                stock = 0


            uid = (
                f"{group_name}_"
                f"{member_name}_"
                f"{session_name}_"
                f"{track}"
            )


            parsed_items.append(
                {
                    "id": uid,
                    "group": group_name,
                    "name": member_name,
                    "session": session_name,
                    "track": track,
                    "quota": stock > 0,
                    "stock": stock,
                    "buy_url": buy_url,
                }
            )


    return parsed_items


# ============================================================
# DISCORD
# ============================================================

def discord_post(
    webhook_url,
    payload,
):

    try:

        response = requests.post(
            webhook_url,
            params={
                "wait": "true",
            },
            json=payload,
            headers={
                "User-Agent":
                "48Group-2Shot-Monitor/1.0",

                "Content-Type":
                "application/json",
            },
            timeout=15,
        )


    except requests.RequestException as exc:

        raise RuntimeError(
            "Gagal terhubung ke Discord: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


    if response.status_code not in (
        200,
        204,
    ):

        raise RuntimeError(
            f"Discord HTTP "
            f"{response.status_code}: "
            f"{response.text[:300]}"
        )


    return response


# ============================================================
# DISCORD EMBED
# ============================================================

def send_embed(
    webhook_url,
    title,
    description,
    color,
    content=None,
    fields=None,
):

    embed = {
        "title": title,
        "description": description,
        "color": color,

        "footer": {
            "text":
            "48Group 2-Shot Monitor "
            "• Automatic System"
        },

        "timestamp":
        datetime.now(
            timezone.utc
        ).isoformat(),
    }


    if fields:

        embed["fields"] = fields


    payload = {
        "embeds": [
            embed
        ],

        "allowed_mentions": {
            "parse": [
                "everyone"
            ]
        },
    }


    if content:

        payload["content"] = content


    return discord_post(
        webhook_url,
        payload,
    )


# ============================================================
# ACTIVATION MESSAGE
# ============================================================

def send_activation_message(
    webhook_url,
):

    return send_embed(
        webhook_url,

        "✅ 48GROUP MONITOR ACTIVATED",

        (
            "Webhook berhasil terhubung.\n\n"

            "• **Restock alert:** realtime\n"
            "• **Daily report:** "
            "08:00 / 12:00 / 20:00 WIB\n"
            "• **Monitoring:** "
            "mengikuti pilihan di dashboard\n\n"

            "Sistem monitoring sekarang aktif."
        ),

        COLOR_GREEN,
    )


# ============================================================
# TEST MESSAGE
# ============================================================

def send_test_message(
    webhook_url,
):

    return send_embed(
        webhook_url,

        "🧪 TEST WEBHOOK BERHASIL",

        (
            "Dashboard berhasil mengirim "
            "pesan ke channel Discord ini."
        ),

        COLOR_GREEN,
    )


# ============================================================
# RESTOCK BROADCAST
# ============================================================

def broadcast_restock(
    item,
    old_stock,
):

    hooks = list_enabled_webhooks(
        item["group"]
    )


    if not hooks:

        log.info(
            "Restock terdeteksi tetapi "
            "tidak ada webhook aktif."
        )

        return


    description = (
        f"> 🏢 **Grup:** `{item['group']}`\n"
        f"> 👤 **Member:** `{item['name']}`\n"
        f"> 🕒 **Sesi:** `{item['session']}`\n"
        f"> 📍 **Jalur:** `{item['track']}`\n"
        f"> 📦 **Stok:** "
        f"`{old_stock} → {item['stock']}`\n\n"
        f"👉 **[BELI TIKET 2-SHOT]"
        f"({item['buy_url']})**"
    )


    for hook in hooks:

        try:

            webhook_url = decrypt_webhook(
                hook[
                    "webhook_url_enc"
                ]
            )


            if hook[
                "mention_everyone"
            ]:

                content = (
                    "@everyone "
                    "🚨 **RESTOCK "
                    "TERDETEKSI!**"
                )

            else:

                content = (
                    "🚨 **RESTOCK "
                    "TERDETEKSI!**"
                )


            send_embed(
                webhook_url,

                "🚨 2-SHOT RESTOCK ALERT!",

                description,

                COLOR_BLUE,

                content=content,
            )


            log.info(
                "Restock dikirim "
                "webhook id=%s",
                hook["id"],
            )


        except Exception:

            log.exception(
                "Gagal mengirim restock "
                "webhook id=%s",
                hook["id"],
            )


# ============================================================
# FORMAT REPORT
# ============================================================

def available_group_text(
    members,
    group_name,
    limit=18,
):

    available = [
        member

        for member in members

        if (
            member["group"]
            == group_name

            and member["stock"] > 0
        )
    ]


    if not available:

        return (
            "❌ Seluruh slot sedang sold out."
        )


    lines = []


    for member in available[:limit]:

        lines.append(
            f"• **{member['name']}** — "
            f"`{member['session']}` | "
            f"`{member['track']}` → "
            f"**{member['stock']}**"
        )


    if len(available) > limit:

        lines.append(
            f"…dan "
            f"{len(available) - limit} "
            f"slot lainnya."
        )


    return "\n".join(
        lines
    )[:1000]


# ============================================================
# SCHEDULED REPORT
# ============================================================

def send_scheduled_report(
    members,
    report_hour,
):

    hooks = list_enabled_webhooks()


    if not hooks:

        return


    jkt_text = available_group_text(
        members,
        "JKT48",
    )


    akb_text = available_group_text(
        members,
        "AKB48",
    )


    for hook in hooks:

        try:

            webhook_url = decrypt_webhook(
                hook[
                    "webhook_url_enc"
                ]
            )


            fields = []


            if hook[
                "notify_jkt"
            ]:

                fields.append(
                    {
                        "name":
                        "🏢 JKT48",

                        "value":
                        jkt_text,

                        "inline":
                        False,
                    }
                )


            if hook[
                "notify_akb"
            ]:

                fields.append(
                    {
                        "name":
                        "🏢 AKB48",

                        "value":
                        akb_text,

                        "inline":
                        False,
                    }
                )


            if not fields:

                continue


            send_embed(
                webhook_url,

                (
                    "📊 REKAP 2-SHOT • "
                    f"{report_hour:02d}:00 WIB"
                ),

                (
                    "Status ketersediaan "
                    "terbaru dari monitor "
                    "48Group."
                ),

                COLOR_PURPLE,

                fields=fields,
            )


        except Exception:

            log.exception(
                "Scheduled report gagal "
                "webhook id=%s",
                hook["id"],
            )


# ============================================================
# SNAPSHOT
# ============================================================

def snapshot():

    with STATE_LOCK:

        return {
            "members":
            list(
                RUNTIME_STATE[
                    "members"
                ]
            ),

            "last_check":
            RUNTIME_STATE[
                "last_check"
            ],

            "last_success":
            RUNTIME_STATE[
                "last_success"
            ],

            "last_error":
            RUNTIME_STATE[
                "last_error"
            ],

            "running":
            RUNTIME_STATE[
                "running"
            ],
        }


# ============================================================
# MONITOR SERVICE
# ============================================================

class MonitorService:

    def __init__(self):

        self.stop_event = (
            threading.Event()
        )

        self.thread = None


        # Waktu berikutnya sebuah group
        # boleh melakukan request.
        self.next_request_time = {
            "JKT48": 0,
            "AKB48": 0,
        }


        self.last_schedule_key = None


        try:

            self.prev_state = (
                load_event_state()
            )

        except Exception:

            log.exception(
                "Gagal membaca "
                "state database."
            )

            self.prev_state = {}


        self.restore_cached_data()


    # ========================================================
    # RESTORE DATABASE
    # ========================================================

    def restore_cached_data(self):

        restored = []


        for uid, row in (
            self.prev_state.items()
        ):

            try:

                group = row[
                    "group_name"
                ]


                event = EVENT_MAP.get(
                    group
                )


                if not event:

                    continue


                stock = int(
                    row["stock"]
                )


                restored.append(
                    {
                        "id": uid,
                        "group": group,

                        "name":
                        row[
                            "member_name"
                        ],

                        "session":
                        row[
                            "session_name"
                        ],

                        "track":
                        row[
                            "track_name"
                        ],

                        "quota":
                        stock > 0,

                        "stock":
                        stock,

                        "buy_url":
                        event[
                            "buy_url"
                        ],
                    }
                )


            except Exception:

                continue


        if restored:

            with STATE_LOCK:

                RUNTIME_STATE[
                    "members"
                ] = restored


            log.info(
                "Cache database dipulihkan: "
                "%s slot.",
                len(restored),
            )


    # ========================================================
    # START
    # ========================================================

    def start(self):

        if (
            self.thread
            and self.thread.is_alive()
        ):

            return


        self.stop_event.clear()


        self.thread = threading.Thread(
            target=self.run,
            daemon=True,
            name="48group-monitor",
        )


        self.thread.start()


    # ========================================================
    # STOP
    # ========================================================

    def stop(self):

        self.stop_event.set()


        if self.thread:

            self.thread.join(
                timeout=10
            )


    # ========================================================
    # LOOP
    # ========================================================

    def run(self):

        with STATE_LOCK:

            RUNTIME_STATE[
                "running"
            ] = True


        log.info(
            "Monitor aktif. "
            "Base interval=%s detik.",
            settings.check_interval,
        )


        try:

            while not (
                self.stop_event.is_set()
            ):

                try:

                    self.poll_once()

                    self.maybe_send_scheduled_report()


                except Exception as exc:

                    log.exception(
                        "Monitor loop error."
                    )


                    with STATE_LOCK:

                        RUNTIME_STATE[
                            "last_error"
                        ] = (
                            f"{type(exc).__name__}: "
                            f"{exc}"
                        )


                self.stop_event.wait(
                    settings.check_interval
                )


        finally:

            with STATE_LOCK:

                RUNTIME_STATE[
                    "running"
                ] = False


    # ========================================================
    # POLL
    # ========================================================

    def poll_once(self):

        jakarta_now = datetime.now(
            ZoneInfo(
                settings.timezone
            )
        )


        current_timestamp = time.time()


        with STATE_LOCK:

            RUNTIME_STATE[
                "last_check"
            ] = (
                jakarta_now.isoformat()
            )


            previous_runtime = list(
                RUNTIME_STATE[
                    "members"
                ]
            )


        # Data dashboard lama.
        dashboard_by_group = {
            "JKT48": [
                member
                for member
                in previous_runtime
                if member["group"]
                == "JKT48"
            ],

            "AKB48": [
                member
                for member
                in previous_runtime
                if member["group"]
                == "AKB48"
            ],
        }


        successful_groups = []

        problems = []


        # ====================================================
        # LOOP GROUP
        # ====================================================

        for event in EVENTS:

            group = event["group"]


            # ================================================
            # COOLDOWN
            # ================================================

            next_allowed = (
                self.next_request_time.get(
                    group,
                    0,
                )
            )


            if (
                current_timestamp
                < next_allowed
            ):

                remaining = int(
                    next_allowed
                    - current_timestamp
                )


                log.info(
                    "%s cooldown, "
                    "%ss tersisa.",
                    group,
                    remaining,
                )


                continue


            # ================================================
            # REQUEST
            # ================================================

            (
                data,
                error,
                status_code,
                retry_after,
            ) = fetch_api(
                event["api_url"]
            )


            # ================================================
            # FAILURE
            # ================================================

            if data is None:

                cooldown = (
                    retry_after
                    if retry_after
                    else 30
                )


                self.next_request_time[
                    group
                ] = (
                    current_timestamp
                    + cooldown
                )


                problem = (
                    f"{group}: {error}"
                )


                problems.append(
                    problem
                )


                log.warning(
                    "%s gagal. "
                    "Cooldown %ss. "
                    "Reason: %s",
                    group,
                    cooldown,
                    error,
                )


                continue


            # ================================================
            # SUCCESS
            # ================================================

            self.next_request_time[
                group
            ] = 0


            parsed = parse_api_data(
                data,
                group,
                event["buy_url"],
            )


            dashboard_by_group[
                group
            ] = parsed


            successful_groups.append(
                group
            )


            log.info(
                "%s BERHASIL - "
                "%s slot.",
                group,
                len(parsed),
            )


            # ================================================
            # CHECK RESTOCK
            # ================================================

            for item in parsed:

                uid = item["id"]


                previous = (
                    self.prev_state.get(
                        uid
                    )
                )


                if previous is not None:

                    try:

                        old_stock = int(
                            previous.get(
                                "stock",
                                0,
                            )
                        )

                    except Exception:

                        old_stock = 0


                    if (
                        old_stock <= 0

                        and item[
                            "stock"
                        ] > 0
                    ):

                        log.warning(
                            "RESTOCK >>> "
                            "%s | %s | "
                            "%s | %s | "
                            "%s -> %s",

                            item[
                                "group"
                            ],

                            item[
                                "name"
                            ],

                            item[
                                "session"
                            ],

                            item[
                                "track"
                            ],

                            old_stock,

                            item[
                                "stock"
                            ],
                        )


                        add_restock_log(
                            item,
                            old_stock,
                            item["stock"],
                        )


                        broadcast_restock(
                            item,
                            old_stock,
                        )


                # ============================================
                # SAVE DATABASE
                # ============================================

                upsert_event_state(
                    item
                )


                self.prev_state[
                    uid
                ] = {
                    "uid":
                    uid,

                    "group_name":
                    item["group"],

                    "member_name":
                    item["name"],

                    "session_name":
                    item["session"],

                    "track_name":
                    item["track"],

                    "stock":
                    item["stock"],
                }


        # ====================================================
        # UPDATE DASHBOARD
        # ====================================================

        combined_members = (
            dashboard_by_group[
                "JKT48"
            ]
            +
            dashboard_by_group[
                "AKB48"
            ]
        )


        with STATE_LOCK:

            RUNTIME_STATE[
                "members"
            ] = combined_members


            if successful_groups:

                RUNTIME_STATE[
                    "last_success"
                ] = (
                    jakarta_now.isoformat()
                )


            if problems:

                RUNTIME_STATE[
                    "last_error"
                ] = (
                    "Sebagian API bermasalah. "
                    + " | ".join(
                        problems
                    )
                )


            elif successful_groups:

                RUNTIME_STATE[
                    "last_error"
                ] = None


        log.info(
            "Polling selesai. "
            "success=%s "
            "dashboard=%s slot.",
            successful_groups,
            len(combined_members),
        )


    # ========================================================
    # SCHEDULER
    # ========================================================

    def maybe_send_scheduled_report(
        self,
    ):

        now = datetime.now(
            ZoneInfo(
                settings.timezone
            )
        )


        if now.hour not in (
            8,
            12,
            20,
        ):

            return


        # Report boleh terkirim
        # pada menit 00 - 04.
        if now.minute >= 5:

            return


        schedule_key = (
            f"{now.date().isoformat()}"
            f"-{now.hour}"
        )


        if (
            self.last_schedule_key
            == schedule_key
        ):

            return


        state = snapshot()


        if not state[
            "last_success"
        ]:

            return


        members = state[
            "members"
        ]


        if not members:

            return


        send_scheduled_report(
            members,
            now.hour,
        )


        self.last_schedule_key = (
            schedule_key
        )


        log.info(
            "Scheduled report "
            "%02d:00 WIB terkirim.",
            now.hour,
        )
