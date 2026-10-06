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

EVENT_MAP = {event["group"]: event for event in EVENTS}
GROUP_NAMES = [event["group"] for event in EVENTS]

COLOR_GREEN = 0x2ECC71
COLOR_RED = 0xE74C3C
COLOR_BLUE = 0x3498DB
COLOR_PURPLE = 0x9B59B6

# Backoff when the origin refuses automated requests.
# Repeated retries against a 403/429 usually make things worse.
BACKOFF_403 = [300, 600, 1200, 1800, 3600]  # 5m, 10m, 20m, 30m, 60m
BACKOFF_429 = [120, 300, 600, 1200, 1800]
NETWORK_BACKOFF = [30, 60, 120, 300]

STATE_LOCK = threading.Lock()
RUNTIME_STATE = {
    "members": [],
    "last_check": None,
    "last_success": None,
    "last_error": None,
    "running": False,
    "groups": {
        group: {
            "status": "cached",
            "last_attempt": None,
            "last_success": None,
            "last_http": None,
            "error": None,
            "cooldown_until_epoch": 0,
            "retry_in": 0,
            "failures": 0,
        }
        for group in GROUP_NAMES
    },
}


def _jakarta_now():
    return datetime.now(ZoneInfo(settings.timezone))


def _iso_now():
    return _jakarta_now().isoformat()


def _copy_group_meta(meta: dict) -> dict:
    copied = dict(meta)
    until = float(copied.get("cooldown_until_epoch") or 0)
    copied["retry_in"] = max(0, int(until - time.time()))
    return copied


def snapshot():
    with STATE_LOCK:
        return {
            "members": list(RUNTIME_STATE["members"]),
            "last_check": RUNTIME_STATE["last_check"],
            "last_success": RUNTIME_STATE["last_success"],
            "last_error": RUNTIME_STATE["last_error"],
            "running": RUNTIME_STATE["running"],
            "groups": {
                group: _copy_group_meta(meta)
                for group, meta in RUNTIME_STATE["groups"].items()
            },
        }


def parse_api_data(response_json, group_name: str, buy_url: str):
    parsed_items = []
    if not isinstance(response_json, dict):
        return parsed_items

    sessions = response_json.get("data", [])
    if not isinstance(sessions, list):
        return parsed_items

    for session_obj in sessions:
        if not isinstance(session_obj, dict):
            continue

        session_name = session_obj.get("label", "-")
        session_members = session_obj.get("session_members", [])
        if not isinstance(session_members, list):
            continue

        for detail in session_members:
            if not isinstance(detail, dict):
                continue

            member_name = detail.get("member_name", "Unknown")
            track = detail.get("label", "-")

            try:
                stock = int(detail.get("available_quota", 0) or 0)
            except (TypeError, ValueError):
                stock = 0

            uid = f"{group_name}_{member_name}_{session_name}_{track}"
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


def discord_post(webhook_url: str, payload: dict):
    try:
        response = requests.post(
            webhook_url,
            params={"wait": "true"},
            json=payload,
            headers={
                "User-Agent": "48Group-2Shot-Monitor/1.0",
                "Content-Type": "application/json",
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Gagal terhubung ke Discord: {type(exc).__name__}: {exc}"
        ) from exc

    if response.status_code not in (200, 204):
        raise RuntimeError(
            f"Discord HTTP {response.status_code}: {response.text[:300]}"
        )
    return response


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
        "footer": {"text": "48Group 2-Shot Monitor • Automatic System"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if fields:
        embed["fields"] = fields

    payload = {
        "embeds": [embed],
        "allowed_mentions": {"parse": ["everyone"]},
    }
    if content:
        payload["content"] = content

    return discord_post(webhook_url, payload)


def send_activation_message(webhook_url: str):
    return send_embed(
        webhook_url,
        "✅ 48GROUP MONITOR ACTIVATED",
        (
            "Webhook berhasil terhubung.\n\n"
            "• **Restock alert:** realtime saat API dapat dipantau\n"
            "• **Daily report:** 08:00 / 12:00 / 20:00 WIB\n"
            "• **Monitoring:** mengikuti pilihan grup di dashboard\n\n"
            "Sistem monitoring sekarang aktif."
        ),
        COLOR_GREEN,
    )


def send_test_message(webhook_url: str):
    return send_embed(
        webhook_url,
        "🧪 TEST WEBHOOK BERHASIL",
        "Dashboard berhasil mengirim pesan ke channel Discord ini.",
        COLOR_GREEN,
    )


def broadcast_restock(item: dict, old_stock: int):
    hooks = list_enabled_webhooks(item["group"])
    if not hooks:
        log.info(
            "Restock %s %s terdeteksi, tetapi tidak ada webhook aktif.",
            item["group"],
            item["name"],
        )
        return

    description = (
        f"> 🏢 **Grup:** `{item['group']}`\n"
        f"> 👤 **Member:** `{item['name']}`\n"
        f"> 🕒 **Sesi:** `{item['session']}`\n"
        f"> 📍 **Jalur:** `{item['track']}`\n"
        f"> 📦 **Stok:** `{old_stock} → {item['stock']}`\n\n"
        f"👉 **[BELI TIKET 2-SHOT]({item['buy_url']})**"
    )

    for hook in hooks:
        try:
            webhook_url = decrypt_webhook(hook["webhook_url_enc"])
            content = (
                "@everyone 🚨 **RESTOCK TERDETEKSI!**"
                if hook["mention_everyone"]
                else "🚨 **RESTOCK TERDETEKSI!**"
            )
            send_embed(
                webhook_url,
                "🚨 2-SHOT RESTOCK ALERT!",
                description,
                COLOR_BLUE,
                content=content,
            )
            log.info("Restock dikirim ke webhook id=%s", hook["id"])
        except Exception:
            log.exception("Gagal mengirim restock webhook id=%s", hook["id"])


def available_group_text(members, group_name, limit=18):
    available = [
        member
        for member in members
        if member["group"] == group_name and member["stock"] > 0
    ]
    if not available:
        return "❌ Seluruh slot sedang sold out."

    lines = [
        f"• **{m['name']}** — `{m['session']}` | `{m['track']}` → **{m['stock']}**"
        for m in available[:limit]
    ]
    if len(available) > limit:
        lines.append(f"…dan {len(available) - limit} slot tersedia lainnya.")
    return "\n".join(lines)[:1000]


def send_scheduled_report(members, report_hour, group_meta=None):
    hooks = list_enabled_webhooks()
    if not hooks:
        return

    group_meta = group_meta or {}
    jkt_text = available_group_text(members, "JKT48")
    akb_text = available_group_text(members, "AKB48")

    for hook in hooks:
        try:
            webhook_url = decrypt_webhook(hook["webhook_url_enc"])
            fields = []

            if hook["notify_jkt"]:
                jkt_status = group_meta.get("JKT48", {}).get("status", "cached")
                suffix = "" if jkt_status == "live" else " ⚠️ cached"
                fields.append(
                    {
                        "name": f"🏢 JKT48{suffix}",
                        "value": jkt_text,
                        "inline": False,
                    }
                )

            if hook["notify_akb"]:
                akb_status = group_meta.get("AKB48", {}).get("status", "cached")
                suffix = "" if akb_status == "live" else " ⚠️ cached"
                fields.append(
                    {
                        "name": f"🏢 AKB48{suffix}",
                        "value": akb_text,
                        "inline": False,
                    }
                )

            if not fields:
                continue

            send_embed(
                webhook_url,
                f"📊 REKAP 2-SHOT • {report_hour:02d}:00 WIB",
                "Status ketersediaan terbaru yang dimiliki monitor 48Group.",
                COLOR_PURPLE,
                fields=fields,
            )
        except Exception:
            log.exception("Scheduled report gagal webhook id=%s", hook["id"])


class MonitorService:
    def __init__(self):
        self.stop_event = threading.Event()
        self.thread = None
        self.last_schedule_key = None

        # One persistent browser-like HTTP session per group.
        # This preserves normal cookies/connections between requests without
        # trying to automate or bypass a Cloudflare challenge.
        self.sessions = {
            group: cffi_requests.Session(impersonate="chrome")
            for group in GROUP_NAMES
        }

        self.failure_counts = {group: 0 for group in GROUP_NAMES}
        self.next_request_time = {group: 0.0 for group in GROUP_NAMES}

        try:
            self.prev_state = load_event_state()
        except Exception:
            log.exception("Gagal membaca event_state database.")
            self.prev_state = {}

        self.restore_cached_data()

    def restore_cached_data(self):
        restored = []
        last_success_by_group = {group: None for group in GROUP_NAMES}

        for uid, row in self.prev_state.items():
            try:
                group = row["group_name"]
                event = EVENT_MAP.get(group)
                if not event:
                    continue

                stock = int(row["stock"])
                restored.append(
                    {
                        "id": uid,
                        "group": group,
                        "name": row["member_name"],
                        "session": row["session_name"],
                        "track": row["track_name"],
                        "quota": stock > 0,
                        "stock": stock,
                        "buy_url": event["buy_url"],
                    }
                )

                updated_at = row.get("updated_at")
                if updated_at and (
                    last_success_by_group[group] is None
                    or updated_at > last_success_by_group[group]
                ):
                    last_success_by_group[group] = updated_at
            except Exception:
                continue

        with STATE_LOCK:
            if restored:
                RUNTIME_STATE["members"] = restored

            latest = None
            for group in GROUP_NAMES:
                cached_at = last_success_by_group[group]
                meta = RUNTIME_STATE["groups"][group]
                meta["status"] = "cached"
                meta["last_success"] = cached_at
                meta["error"] = "Menampilkan data terakhir dari database."
                if cached_at and (latest is None or cached_at > latest):
                    latest = cached_at

            RUNTIME_STATE["last_success"] = latest

        if restored:
            log.info("Cache database dipulihkan: %s slot.", len(restored))

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self.run,
            daemon=True,
            name="48group-monitor",
        )
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=10)

    def _backoff_seconds(self, status_code, failure_count, retry_after=None):
        idx = max(0, failure_count - 1)

        if status_code == 403:
            return BACKOFF_403[min(idx, len(BACKOFF_403) - 1)]
        if status_code == 429:
            if retry_after:
                return max(60, int(retry_after))
            return BACKOFF_429[min(idx, len(BACKOFF_429) - 1)]
        return NETWORK_BACKOFF[min(idx, len(NETWORK_BACKOFF) - 1)]

    def _fetch_group(self, event):
        group = event["group"]
        now_epoch = time.time()

        if now_epoch < self.next_request_time[group]:
            remaining = int(self.next_request_time[group] - now_epoch)
            return {
                "kind": "cooldown",
                "group": group,
                "retry_in": remaining,
                "status_code": RUNTIME_STATE["groups"][group].get("last_http"),
            }

        attempt_iso = _iso_now()
        with STATE_LOCK:
            RUNTIME_STATE["groups"][group]["last_attempt"] = attempt_iso

        try:
            response = self.sessions[group].get(
                event["api_url"],
                timeout=20,
            )
            status_code = int(response.status_code)
            log.info("%s API HTTP %s", group, status_code)

            if status_code == 200:
                try:
                    data = response.json()
                except Exception as exc:
                    raise RuntimeError(f"HTTP 200 tetapi JSON invalid: {exc}") from exc

                self.failure_counts[group] = 0
                self.next_request_time[group] = 0
                return {
                    "kind": "success",
                    "group": group,
                    "data": data,
                    "status_code": 200,
                    "attempt_iso": attempt_iso,
                }

            self.failure_counts[group] += 1
            retry_after = None
            if status_code == 429:
                try:
                    retry_after = response.headers.get("Retry-After")
                    if retry_after is not None:
                        retry_after = int(float(retry_after))
                except Exception:
                    retry_after = None

            cooldown = self._backoff_seconds(
                status_code,
                self.failure_counts[group],
                retry_after,
            )
            self.next_request_time[group] = now_epoch + cooldown

            if status_code == 403:
                error = "HTTP 403 - endpoint menolak request otomatis"
            elif status_code == 429:
                error = "HTTP 429 - rate limit"
            else:
                error = f"HTTP {status_code}"

            return {
                "kind": "http_error",
                "group": group,
                "status_code": status_code,
                "error": error,
                "cooldown": cooldown,
                "attempt_iso": attempt_iso,
            }

        except Exception as exc:
            self.failure_counts[group] += 1
            cooldown = self._backoff_seconds(
                None,
                self.failure_counts[group],
            )
            self.next_request_time[group] = now_epoch + cooldown
            return {
                "kind": "network_error",
                "group": group,
                "status_code": None,
                "error": f"{type(exc).__name__}: {exc}",
                "cooldown": cooldown,
                "attempt_iso": attempt_iso,
            }

    def _set_group_success(self, group, success_iso):
        with STATE_LOCK:
            meta = RUNTIME_STATE["groups"][group]
            meta["status"] = "live"
            meta["last_success"] = success_iso
            meta["last_http"] = 200
            meta["error"] = None
            meta["cooldown_until_epoch"] = 0
            meta["retry_in"] = 0
            meta["failures"] = 0

    def _set_group_failure(self, group, result, has_cached_data):
        now_epoch = time.time()
        cooldown = int(result.get("cooldown") or result.get("retry_in") or 0)
        until = self.next_request_time[group] if cooldown else 0
        kind = result["kind"]

        if kind == "cooldown":
            status = "cooldown" if has_cached_data else "error"
            error = RUNTIME_STATE["groups"][group].get("error") or "Menunggu retry."
        elif result.get("status_code") in (403, 429):
            status = "cooldown" if has_cached_data else "error"
            error = result.get("error")
        else:
            status = "cached" if has_cached_data else "error"
            error = result.get("error")

        with STATE_LOCK:
            meta = RUNTIME_STATE["groups"][group]
            meta["status"] = status
            if result.get("status_code") is not None:
                meta["last_http"] = result.get("status_code")
            meta["error"] = error
            meta["cooldown_until_epoch"] = until
            meta["retry_in"] = max(0, int(until - now_epoch)) if until else 0
            meta["failures"] = self.failure_counts[group]

    def run(self):
        with STATE_LOCK:
            RUNTIME_STATE["running"] = True

        log.info("Monitor aktif. Base interval=%ss", settings.check_interval)

        try:
            while not self.stop_event.is_set():
                try:
                    self.poll_once()
                    self.maybe_send_scheduled_report()
                except Exception as exc:
                    log.exception("Monitor loop error")
                    with STATE_LOCK:
                        RUNTIME_STATE["last_error"] = f"{type(exc).__name__}: {exc}"

                self.stop_event.wait(settings.check_interval)
        finally:
            with STATE_LOCK:
                RUNTIME_STATE["running"] = False

    def poll_once(self):
        check_iso = _iso_now()

        with STATE_LOCK:
            RUNTIME_STATE["last_check"] = check_iso
            previous_runtime = list(RUNTIME_STATE["members"])

        dashboard_by_group = {
            group: [m for m in previous_runtime if m["group"] == group]
            for group in GROUP_NAMES
        }

        loop_errors = []
        successful_groups = []

        for index, event in enumerate(EVENTS):
            group = event["group"]
            has_cached_data = bool(dashboard_by_group[group])
            result = self._fetch_group(event)

            if result["kind"] == "success":
                parsed = parse_api_data(result["data"], group, event["buy_url"])
                dashboard_by_group[group] = parsed
                successful_groups.append(group)
                success_iso = result.get("attempt_iso") or check_iso
                self._set_group_success(group, success_iso)

                for item in parsed:
                    uid = item["id"]
                    previous = self.prev_state.get(uid)
                    if previous is not None:
                        try:
                            old_stock = int(previous.get("stock", 0))
                        except Exception:
                            old_stock = 0

                        if old_stock <= 0 and item["stock"] > 0:
                            log.warning(
                                "RESTOCK %s | %s | %s | %s | %s -> %s",
                                item["group"],
                                item["name"],
                                item["session"],
                                item["track"],
                                old_stock,
                                item["stock"],
                            )
                            add_restock_log(item, old_stock, item["stock"])
                            broadcast_restock(item, old_stock)

                    upsert_event_state(item)
                    self.prev_state[uid] = {
                        "uid": uid,
                        "group_name": item["group"],
                        "member_name": item["name"],
                        "session_name": item["session"],
                        "track_name": item["track"],
                        "stock": item["stock"],
                        "updated_at": success_iso,
                    }

                log.info("%s live: %s slot", group, len(parsed))
            else:
                self._set_group_failure(group, result, has_cached_data)
                if result["kind"] != "cooldown":
                    loop_errors.append(f"{group}: {result.get('error', 'fetch gagal')}")
                    log.warning(
                        "%s gagal; status=%s; retry=%ss",
                        group,
                        result.get("status_code"),
                        result.get("cooldown", 0),
                    )

            # Do not burst both endpoints at the exact same instant.
            if index < len(EVENTS) - 1 and not self.stop_event.is_set():
                self.stop_event.wait(1.5)

        combined = []
        for group in GROUP_NAMES:
            combined.extend(dashboard_by_group[group])

        with STATE_LOCK:
            RUNTIME_STATE["members"] = combined

            last_successes = [
                meta.get("last_success")
                for meta in RUNTIME_STATE["groups"].values()
                if meta.get("last_success")
            ]
            RUNTIME_STATE["last_success"] = max(last_successes) if last_successes else None

            if loop_errors:
                RUNTIME_STATE["last_error"] = " | ".join(loop_errors)
            else:
                # If a group is still in cooldown, show a short summary rather
                # than dumping the Cloudflare HTML challenge into the UI.
                waiting = []
                for group, meta in RUNTIME_STATE["groups"].items():
                    if meta.get("status") in ("cooldown", "error") and meta.get("error"):
                        waiting.append(f"{group}: {meta['error']}")
                RUNTIME_STATE["last_error"] = " | ".join(waiting) if waiting else None

        log.info(
            "Polling selesai. live=%s dashboard=%s slot",
            successful_groups,
            len(combined),
        )

    def maybe_send_scheduled_report(self):
        now = _jakarta_now()
        if now.hour not in (8, 12, 20):
            return
        if now.minute >= 5:
            return

        schedule_key = f"{now.date().isoformat()}-{now.hour}"
        if self.last_schedule_key == schedule_key:
            return

        state = snapshot()
        if not state["members"]:
            return

        send_scheduled_report(
            state["members"],
            now.hour,
            group_meta=state["groups"],
        )
        self.last_schedule_key = schedule_key
        log.info("Scheduled report %02d:00 WIB terkirim.", now.hour)
