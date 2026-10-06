import logging
import threading
import time
from datetime import datetime
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


# ============================================================
# DISCORD COLORS
# ============================================================

COLOR_GREEN = 0x2ECC71
COLOR_RED = 0xE74C3C
COLOR_BLUE = 0x3498DB
COLOR_PURPLE = 0x9B59B6


# ============================================================
# GLOBAL RUNTIME STATE
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
# HTTP HEADERS
# ============================================================

BROWSER_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://jkt48.com/",
    "Origin": "https://jkt48.com",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


# ============================================================
# API FETCH
# ============================================================

def fetch_api(url: str):
    """
    Mengambil API menggunakan curl_cffi terlebih dahulu.

    Jika gagal, mencoba fallback requests biasa.

    Retry dilakukan agar error jaringan sementara tidak langsung
    membuat monitor kehilangan data.
    """

    last_error = None

    # --------------------------------------------------------
    # METHOD 1 - CURL_CFFI
    # --------------------------------------------------------

    for attempt in range(1, 4):

        try:

            log.info(
                "API request curl_cffi attempt=%s url=%s",
                attempt,
                url,
            )

            response = cffi_requests.get(
                url,
                headers=BROWSER_HEADERS,
                impersonate="chrome",
                timeout=20,
            )

            log.info(
                "API response curl_cffi status=%s url=%s",
                response.status_code,
                url,
            )

            if response.status_code == 200:

                try:
                    return response.json()

                except Exception as exc:

                    preview = response.text[:300]

                    raise RuntimeError(
                        f"Response bukan JSON valid: {exc}. "
                        f"Preview: {preview}"
                    )

            last_error = (
                f"HTTP {response.status_code} "
                f"{response.text[:250]}"
            )

        except Exception as exc:

            last_error = (
                f"{type(exc).__name__}: {exc}"
            )

            log.warning(
                "curl_cffi gagal attempt=%s: %s",
                attempt,
                last_error,
            )

        time.sleep(1.5)


    # --------------------------------------------------------
    # METHOD 2 - REQUESTS FALLBACK
    # --------------------------------------------------------

    try:

        log.info(
            "Mencoba requests fallback: %s",
            url,
        )

        response = requests.get(
            url,
            headers={
                **BROWSER_HEADERS,
                "User-Agent": (
                    "Mozilla/5.0 "
                    "(Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/140.0 Safari/537.36"
                ),
            },
            timeout=20,
        )

        log.info(
            "API fallback response status=%s",
            response.status_code,
        )

        if response.status_code == 200:

            return response.json()

        last_error = (
            f"fallback HTTP {response.status_code}: "
            f"{response.text[:250]}"
        )

    except Exception as exc:

        last_error = (
            f"fallback {type(exc).__name__}: {exc}"
        )


    # --------------------------------------------------------
    # TOTAL FAILURE
    # --------------------------------------------------------

    log.error(
        "API benar-benar gagal: %s | %s",
        url,
        last_error,
    )

    return None


# ============================================================
# PARSE API
# ============================================================

def parse_api_data(
    response_json,
    group_name: str,
    buy_url: str,
):

    parsed = []

    if not response_json:
        return parsed

    sessions = response_json.get(
        "data",
        [],
    )

    if not isinstance(sessions, list):

        log.warning(
            "%s API data bukan list.",
            group_name,
        )

        return parsed


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


        for detail in session_members:

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


            parsed.append(
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


    return parsed


# ============================================================
# GET ALL GROUP DATA
# ============================================================

def get_all_members_data():

    all_members = []

    successful_groups = []

    errors = []


    for event in EVENTS:

        group = event["group"]

        log.info(
            "Mengambil data %s...",
            group,
        )


        data = fetch_api(
            event["api_url"]
        )


        if data is None:

            errors.append(
                f"{group}: gagal fetch API"
            )

            continue


        parsed = parse_api_data(
            data,
            group,
            event["buy_url"],
        )


        # API berhasil meskipun parsed bisa kosong.
        successful_groups.append(
            group
        )


        all_members.extend(
            parsed
        )


        log.info(
            "%s berhasil: %s slot terbaca",
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

    headers = {
        "User-Agent":
        "48Group-2Shot-Monitor/1.0",
        "Content-Type":
        "application/json",
    }


    try:

        response = requests.post(
            webhook_url,
            params={
                "wait": "true"
            },
            json=payload,
            headers=headers,
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
            f"{response.text[:500]}"
        )


    return response


# ============================================================
# SEND DISCORD EMBED
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
            "48Group 2-Shot Monitor • Automatic System"
        },
        "timestamp":
        datetime.utcnow().isoformat()
        + "Z",
    }


    if fields:

        embed["fields"] = fields


    payload = {
        "embeds": [
            embed
        ],

        # @everyone diperbolehkan pada payload.
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

    send_embed(
        webhook_url,
        "✅ 48GROUP MONITOR ACTIVATED",
        (
            "Webhook berhasil terhubung.\n\n"
            "• **Restock alert:** realtime\n"
            "• **Daily report:** "
            "08:00 / 12:00 / 20:00 WIB\n"
            "• **Monitoring:** "
            "JKT48 2-Shot & AKB48 2-Shot\n\n"
            "Sistem monitoring sekarang aktif."
        ),
        COLOR_GREEN,
    )


# ============================================================
# TEST MESSAGE
# ============================================================

def send_test_message(
    webhook_url: str,
):

    send_embed(
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
    item: dict,
    old_stock: int,
):

    hooks = list_enabled_webhooks(
        item["group"]
    )


    if not hooks:

        log.info(
            "Restock terdeteksi tetapi "
            "tidak ada webhook aktif untuk %s.",
            item["group"],
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
                "Restock dikirim webhook id=%s",
                hook["id"],
            )


        except Exception:

            log.exception(
                "Gagal mengirim restock "
                "ke webhook id=%s",
                hook["id"],
            )


# ============================================================
# REPORT TEXT
# ============================================================

def available_group_text(
    members,
    group_name,
    limit=18,
):

    available = [
        m
        for m in members
        if (
            m["group"]
            == group_name
            and m["stock"] > 0
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
            f"slot tersedia lainnya."
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


            if hook["notify_jkt"]:

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


            if hook["notify_akb"]:

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


            send_embed(
                webhook_url,
                (
                    f"📊 REKAP 2-SHOT • "
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

        try:

            self.prev_state = (
                load_event_state()
            )

        except Exception:

            self.prev_state = {}


        self.last_schedule_key = None


    # --------------------------------------------------------
    # START
    # --------------------------------------------------------

    def start(self):

        if (
            self.thread
            and self.thread.is_alive()
        ):

            return


        self.thread = threading.Thread(
            target=self.run,
            daemon=True,
            name="48group-monitor",
        )


        self.thread.start()


    # --------------------------------------------------------
    # STOP
    # --------------------------------------------------------

    def stop(self):

        self.stop_event.set()


        if self.thread:

            self.thread.join(
                timeout=10
            )


    # --------------------------------------------------------
    # MAIN LOOP
    # --------------------------------------------------------

    def run(self):

        with STATE_LOCK:

            RUNTIME_STATE[
                "running"
            ] = True


        log.info(
            "Monitor dimulai; interval=%ss",
            settings.check_interval,
        )


        # Jalankan langsung.
        # Jangan tunggu interval pertama.
        while not self.stop_event.is_set():

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
                    ] = str(exc)


            self.stop_event.wait(
                settings.check_interval
            )


        with STATE_LOCK:

            RUNTIME_STATE[
                "running"
            ] = False


    # --------------------------------------------------------
    # POLL
    # --------------------------------------------------------

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
            members,
            successful_groups,
            errors,
        ) = get_all_members_data()


        # ----------------------------------------------------
        # SEMUA API GAGAL
        # ----------------------------------------------------

        if not successful_groups:

            error_text = (
                "Semua endpoint API gagal "
                "diakses."
            )


            if errors:

                error_text += (
                    " "
                    + " | ".join(
                        errors
                    )
                )


            with STATE_LOCK:

                RUNTIME_STATE[
                    "last_error"
                ] = error_text


            log.error(
                error_text
            )

            return


        # ----------------------------------------------------
        # SETIDAKNYA 1 API BERHASIL
        # ----------------------------------------------------

        current_state = {
            m["id"]: m
            for m in members
        }


        # ----------------------------------------------------
        # FIRST BASELINE
        # ----------------------------------------------------

        if not self.prev_state:

            log.info(
                "Membuat baseline awal "
                "%s slot.",
                len(members),
            )


            for item in members:

                upsert_event_state(
                    item
                )


            self.prev_state = {
                item["id"]: {
                    "stock":
                    item["stock"]
                }
                for item in members
            }


        else:

            # ------------------------------------------------
            # COMPARE STATE
            # ------------------------------------------------

            for (
                uid,
                item,
            ) in current_state.items():

                previous = (
                    self.prev_state.get(
                        uid
                    )
                )


                if previous is not None:

                    old_stock = int(
                        previous.get(
                            "stock",
                            0,
                        )
                    )


                    # ========================================
                    # RESTOCK
                    # ========================================

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


                upsert_event_state(
                    item
                )


            self.prev_state = {
                item["id"]: {
                    "stock":
                    item["stock"]
                }
                for item in members
            }


        # ----------------------------------------------------
        # DASHBOARD UPDATE
        # ----------------------------------------------------

        with STATE_LOCK:

            RUNTIME_STATE[
                "members"
            ] = members


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

                missing = [
                    e["group"]
                    for e in EVENTS
                    if e["group"]
                    not in successful_groups
                ]


                RUNTIME_STATE[
                    "last_error"
                ] = (
                    "API sebagian gagal: "
                    + ", ".join(
                        missing
                    )
                )


        log.info(
            "API update selesai. "
            "Groups=%s Total=%s",
            successful_groups,
            len(members),
        )


    # --------------------------------------------------------
    # SCHEDULER
    # --------------------------------------------------------

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


        # Window 5 menit agar deployment/restart sedikit terlambat
        # masih dapat mengirim report.
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


        current = snapshot()[
            "members"
        ]


        if not current:

            return


        send_scheduled_report(
            current,
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
