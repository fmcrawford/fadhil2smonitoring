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


log = logging.getLogger("48group.monitor")


# ============================================================
# EVENT CONFIG
# ============================================================

EVENTS = [
    {
        "group": "JKT48",
        "api_url": "https://jkt48.com/api/v1/exclusives/EX5B99/bonus?lang=id",
        "buy_url": "https://jkt48.com/purchase/exclusive?code=EX5B99",
    },
    {
        "group": "AKB48",
        "api_url": "https://jkt48.com/api/v1/exclusives/EXD1A1/bonus?lang=id",
        "buy_url": "https://jkt48.com/purchase/exclusive?code=EXD1A1",
    },
]

EVENT_BY_GROUP = {
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
# API HELPERS
# ============================================================

def response_preview(response, limit=300):
    try:
        text = response.text or ""

        return (
            text[:limit]
            .replace("\n", " ")
            .replace("\r", " ")
        )

    except Exception:
        return ""


def fetch_api(url: str):
    """
    Urutan request:

    1. curl_cffi langsung
    2. curl_cffi Session + membuka homepage
    3. requests biasa sebagai fallback

    Return:
        (data_json, None) jika berhasil
        (None, error_message) jika gagal
    """

    errors = []

    # ========================================================
    # METHOD 1
    # curl_cffi langsung
    # Ini metode yang sebelumnya berhasil di bot lama.
    # ========================================================

    for attempt in range(1, 3):

        try:

            log.info(
                "API direct attempt=%s | %s",
                attempt,
                url,
            )

            response = cffi_requests.get(
                url,
                impersonate="chrome",
                timeout=20,
            )

            log.info(
                "API direct status=%s",
                response.status_code,
            )

            if response.status_code == 200:

                try:

                    data = response.json()

                    return data, None

                except Exception as exc:

                    error = (
                        "HTTP 200 tetapi JSON invalid: "
                        f"{type(exc).__name__}: {exc}"
                    )

                    log.error(error)

                    return None, error


            error = (
                f"direct HTTP "
                f"{response.status_code}; "
                f"response="
                f"{response_preview(response)}"
            )

            errors.append(error)

            log.warning(error)


        except Exception as exc:

            error = (
                f"direct "
                f"{type(exc).__name__}: "
                f"{exc}"
            )

            errors.append(error)

            log.warning(error)


        if attempt < 2:
            time.sleep(1)


    # ========================================================
    # METHOD 2
    # Browser session + homepage warm-up
    # ========================================================

    try:

        log.info(
            "Mencoba curl_cffi browser session..."
        )

        session = cffi_requests.Session(
            impersonate="chrome"
        )


        try:

            home = session.get(
                "https://jkt48.com/",
                timeout=20,
            )

            log.info(
                "Homepage status=%s",
                home.status_code,
            )

        except Exception as exc:

            log.warning(
                "Homepage warm-up gagal: %s: %s",
                type(exc).__name__,
                exc,
            )


        response = session.get(
            url,
            timeout=20,
        )


        log.info(
            "API session status=%s",
            response.status_code,
        )


        if response.status_code == 200:

            try:

                return (
                    response.json(),
                    None,
                )

            except Exception as exc:

                error = (
                    "Session mendapatkan HTTP 200 "
                    "tetapi JSON invalid: "
                    f"{type(exc).__name__}: {exc}"
                )

                log.error(error)

                return None, error


        error = (
            f"session HTTP "
            f"{response.status_code}; "
            f"response="
            f"{response_preview(response)}"
        )

        errors.append(error)

        log.warning(error)


    except Exception as exc:

        error = (
            f"session "
            f"{type(exc).__name__}: "
            f"{exc}"
        )

        errors.append(error)

        log.warning(error)


    # ========================================================
    # METHOD 3
    # Standard requests fallback
    # ========================================================

    try:

        log.info(
            "Mencoba requests fallback..."
        )


        response = requests.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 "
                    "(Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/140.0.0.0 "
                    "Safari/537.36"
                ),
                "Accept": (
                    "application/json,"
                    "text/plain,*/*"
                ),
                "Referer": (
                    "https://jkt48.com/"
                ),
            },
            timeout=20,
        )


        log.info(
            "Requests fallback status=%s",
            response.status_code,
        )


        if response.status_code == 200:

            try:

                return (
                    response.json(),
                    None,
                )

            except Exception as exc:

                error = (
                    "Fallback HTTP 200 "
                    "tetapi JSON invalid: "
                    f"{type(exc).__name__}: {exc}"
                )

                log.error(error)

                return None, error


        error = (
            f"fallback HTTP "
            f"{response.status_code}; "
            f"response="
            f"{response_preview(response)}"
        )

        errors.append(error)

        log.warning(error)


    except Exception as exc:

        error = (
            f"fallback "
            f"{type(exc).__name__}: "
            f"{exc}"
        )

        errors.append(error)

        log.warning(error)


    # ========================================================
    # TOTAL FAILURE
    # ========================================================

    final_error = " || ".join(errors)


    if len(final_error) > 700:

        final_error = (
            final_error[:700]
            + "..."
        )


    if not final_error:

        final_error = "Unknown API error"


    return (
        None,
        final_error,
    )


# ============================================================
# PARSE API
# ============================================================

def parse_api_data(
    response_json,
    group_name: str,
    buy_url: str,
):

    parsed_items = []


    if not isinstance(
        response_json,
        dict,
    ):

        log.warning(
            "%s response bukan object JSON.",
            group_name,
        )

        return parsed_items


    sessions = response_json.get(
        "data",
        [],
    )


    if not isinstance(
        sessions,
        list,
    ):

        log.warning(
            "%s field data bukan list.",
            group_name,
        )

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


        members = session_obj.get(
            "session_members",
            [],
        )


        if not isinstance(
            members,
            list,
        ):
            continue


        for detail in members:

            if not isinstance(
                detail,
                dict,
            ):
                continue


            track = detail.get(
                "label",
                "-",
            )


            member_name = detail.get(
                "member_name",
                "Unknown",
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
# GET ALL API DATA
# ============================================================

def get_all_members_data():

    all_members = []

    successful_groups = set()

    errors = {}


    for event in EVENTS:

        group = event["group"]


        log.info(
            "================================"
        )

        log.info(
            "Mengambil data %s...",
            group,
        )


        data, error = fetch_api(
            event["api_url"]
        )


        if data is None:

            errors[group] = (
                error
                or "Fetch gagal"
            )


            log.error(
                "%s gagal: %s",
                group,
                errors[group],
            )


            continue


        parsed = parse_api_data(
            data,
            group,
            event["buy_url"],
        )


        successful_groups.add(
            group
        )


        all_members.extend(
            parsed
        )


        log.info(
            "%s BERHASIL: %s slot terbaca",
            group,
            len(parsed),
        )


    return (
        all_members,
        successful_groups,
        errors,
    )


# ============================================================
# DISCORD REQUEST
# ============================================================

def discord_post(
    webhook_url: str,
    payload: dict,
):

    try:

        response = requests.post(
            webhook_url,
            params={
                "wait": "true"
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
            f"{type(exc).__name__}: "
            f"{exc}"
        ) from exc


    if response.status_code not in (
        200,
        204,
    ):

        raise RuntimeError(
            f"Discord HTTP "
            f"{response.status_code}: "
            f"{response.text[:500]}"
        )


    return response


# ============================================================
# DISCORD EMBED
# ============================================================

def send_embed(
    webhook_url: str,
    title: str,
    description: str,
    color: int,
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
    webhook_url: str,
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
            "mengikuti pilihan grup "
            "di dashboard\n\n"

            "Sistem monitoring "
            "sekarang aktif."
        ),

        COLOR_GREEN,
    )


# ============================================================
# TEST MESSAGE
# ============================================================

def send_test_message(
    webhook_url: str,
):

    return send_embed(

        webhook_url,

        "🧪 TEST WEBHOOK BERHASIL",

        (
            "Dashboard berhasil "
            "mengirim pesan ke "
            "channel Discord ini."
        ),

        COLOR_GREEN,
    )


# ============================================================
# BROADCAST RESTOCK
# ============================================================

def broadcast_restock(
    item: dict,
    old_stock: int,
):

    hooks = list_enabled_webhooks(
        item["group"]
    )


    if not hooks:

        log.info(
            "Restock %s %s terdeteksi, "
            "tetapi tidak ada webhook aktif.",
            item["group"],
            item["name"],
        )

        return


    description = (

        f"> 🏢 **Grup:** "
        f"`{item['group']}`\n"

        f"> 👤 **Member:** "
        f"`{item['name']}`\n"

        f"> 🕒 **Sesi:** "
        f"`{item['session']}`\n"

        f"> 📍 **Jalur:** "
        f"`{item['track']}`\n"

        f"> 📦 **Stok:** "
        f"`{old_stock} → "
        f"{item['stock']}`\n\n"

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
                "ke webhook id=%s",
                hook["id"],
            )


        except Exception:

            log.exception(
                "Gagal mengirim restock "
                "ke webhook id=%s",
                hook["id"],
            )


# ============================================================
# FORMAT DAILY REPORT
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

            and member["stock"]
            > 0
        )
    ]


    if not available:

        return (
            "❌ Seluruh slot "
            "sedang sold out."
        )


    lines = []


    for member in available[
        :limit
    ]:

        lines.append(

            f"• **{member['name']}** — "

            f"`{member['session']}` | "

            f"`{member['track']}` → "

            f"**{member['stock']}**"
        )


    if len(
        available
    ) > limit:

        lines.append(

            f"…dan "
            f"{len(available) - limit} "
            f"slot tersedia lainnya."
        )


    return "\n".join(
        lines
    )[:1000]


# ============================================================
# DAILY SCHEDULED REPORT
# ============================================================

def send_scheduled_report(
    members,
    report_hour,
):

    hooks = (
        list_enabled_webhooks()
    )


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
                "Gagal mengirim "
                "scheduled report "
                "ke webhook id=%s",
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

        self.last_schedule_key = None


        try:

            self.prev_state = (
                load_event_state()
            )

        except Exception:

            log.exception(
                "Gagal membaca "
                "event_state database."
            )

            self.prev_state = {}


        self.restore_runtime_from_database()


    # ========================================================
    # RESTORE DB CACHE
    # ========================================================

    def restore_runtime_from_database(
        self,
    ):

        if not self.prev_state:

            return


        restored = []


        for (
            uid,
            row,
        ) in self.prev_state.items():

            try:

                group_name = row[
                    "group_name"
                ]


                event = (
                    EVENT_BY_GROUP.get(
                        group_name
                    )
                )


                if not event:

                    continue


                stock = int(
                    row["stock"]
                )


                restored.append(
                    {
                        "id": uid,

                        "group":
                        group_name,

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
                "Memulihkan %s slot "
                "dari database.",
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
    # MAIN LOOP
    # ========================================================

    def run(self):

        with STATE_LOCK:

            RUNTIME_STATE[
                "running"
            ] = True


        log.info(
            "Monitor dimulai; "
            "interval=%ss",
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
                        "Monitor loop error"
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
    # POLL API
    # ========================================================

    def poll_once(self):

        now = datetime.now(

            ZoneInfo(
                settings.timezone
            )
        )


        with STATE_LOCK:

            RUNTIME_STATE[
                "last_check"
            ] = now.isoformat()


        (
            fetched_members,
            successful_groups,
            errors,
        ) = get_all_members_data()


        # ====================================================
        # SEMUA API GAGAL
        # ====================================================

        if not successful_groups:

            error_parts = []


            for group in (
                "JKT48",
                "AKB48",
            ):

                if group in errors:

                    error_parts.append(
                        f"{group}: "
                        f"{errors[group]}"
                    )


            error_text = (
                "Semua endpoint API "
                "gagal diakses."
            )


            if error_parts:

                error_text += (
                    " "
                    + " | ".join(
                        error_parts
                    )
                )


            if len(
                error_text
            ) > 900:

                error_text = (
                    error_text[:900]
                    + "..."
                )


            with STATE_LOCK:

                RUNTIME_STATE[
                    "last_error"
                ] = error_text


            log.error(
                error_text
            )


            return


        # ====================================================
        # AMBIL RUNTIME DATA LAMA
        # ====================================================

        with STATE_LOCK:

            old_runtime_members = list(

                RUNTIME_STATE[
                    "members"
                ]
            )


        # ====================================================
        # JIKA SALAH SATU API GAGAL,
        # PERTAHANKAN DATA TERAKHIR GRUP TERSEBUT
        # ====================================================

        preserved_members = [

            member

            for member
            in old_runtime_members

            if member["group"]
            not in successful_groups
        ]


        members_for_dashboard = (

            fetched_members
            + preserved_members
        )


        fetched_state = {

            item["id"]: item

            for item
            in fetched_members
        }


        # ====================================================
        # BASELINE PERTAMA
        # ====================================================

        if not self.prev_state:

            log.info(
                "Membuat baseline awal "
                "%s slot.",
                len(fetched_members),
            )


            for item in fetched_members:

                upsert_event_state(
                    item
                )


            self.prev_state = {}


            for item in fetched_members:

                self.prev_state[
                    item["id"]
                ] = {

                    "uid":
                    item["id"],

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


        else:

            # =================================================
            # COMPARE STOCK
            # =================================================

            for (
                uid,
                item,
            ) in fetched_state.items():

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


                    # =========================================
                    # RESTOCK
                    #
                    # SEBELUMNYA 0
                    # SEKARANG > 0
                    # =========================================

                    if (
                        old_stock <= 0

                        and item[
                            "stock"
                        ] > 0
                    ):

                        log.warning(

                            "RESTOCK: "
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


                # Simpan state terbaru
                upsert_event_state(
                    item
                )


                self.prev_state[
                    uid
                ] = {

                    "uid":
                    item["id"],

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

        with STATE_LOCK:

            RUNTIME_STATE[
                "members"
            ] = (
                members_for_dashboard
            )


            RUNTIME_STATE[
                "last_success"
            ] = now.isoformat()


            if len(
                successful_groups
            ) == len(EVENTS):

                RUNTIME_STATE[
                    "last_error"
                ] = None


            else:

                failed_groups = [

                    event["group"]

                    for event
                    in EVENTS

                    if event["group"]
                    not in successful_groups
                ]


                details = []


                for group in failed_groups:

                    if group in errors:

                        details.append(

                            f"{group}: "
                            f"{errors[group]}"
                        )


                partial_error = (

                    "Sebagian API gagal. "

                    + " | ".join(
                        details
                    )
                )


                if len(
                    partial_error
                ) > 900:

                    partial_error = (

                        partial_error[:900]
                        + "..."
                    )


                RUNTIME_STATE[
                    "last_error"
                ] = (
                    partial_error
                )


        log.info(
            "API update selesai. "
            "success=%s "
            "total_dashboard=%s",

            sorted(
                successful_groups
            ),

            len(
                members_for_dashboard
            ),
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


        # Hanya jam ini
        if now.hour not in (
            8,
            12,
            20,
        ):

            return


        # Window 5 menit
        #
        # 08:00 - 08:04
        # 12:00 - 12:04
        # 20:00 - 20:04
        #
        # Berguna kalau polling sedikit terlambat.
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
