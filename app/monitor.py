import logging
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

from .config import settings
from .db import (
    add_restock_log,
    list_enabled_webhooks,
    load_event_state,
    upsert_event_state,
)
from .security import decrypt_webhook


log = logging.getLogger("48group.monitor")

EVENTS = {
    "JKT48": {
        "buy_url": "https://jkt48.com/purchase/exclusive?code=EX5B99",
    },
    "AKB48": {
        "buy_url": "https://jkt48.com/purchase/exclusive?code=EXD1A1",
    },
}

GROUP_NAMES = ["JKT48", "AKB48"]

COLOR_GREEN = 0x2ECC71
COLOR_BLUE = 0x3498DB
COLOR_PURPLE = 0x9B59B6

STATE_LOCK = threading.Lock()

RUNTIME_STATE = {
    "members": [],
    "last_check": None,
    "last_success": None,
    "last_error": None,
    "running": False,
    "collector_last_seen": None,
    "groups": {
        group: {
            "status": "cached",
            "last_attempt": None,
            "last_success": None,
            "last_http": None,
            "error": "Menunggu collector lokal.",
            "retry_in": 0,
        }
        for group in GROUP_NAMES
    },
}


def _jakarta_now():
    return datetime.now(ZoneInfo(settings.timezone))


def _iso_now():
    return _jakarta_now().isoformat()


def _parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def snapshot():
    with STATE_LOCK:
        return {
            "members": list(RUNTIME_STATE["members"]),
            "last_check": RUNTIME_STATE["last_check"],
            "last_success": RUNTIME_STATE["last_success"],
            "last_error": RUNTIME_STATE["last_error"],
            "running": RUNTIME_STATE["running"],
            "collector_last_seen": RUNTIME_STATE["collector_last_seen"],
            "groups": {
                group: dict(meta)
                for group, meta in RUNTIME_STATE["groups"].items()
            },
        }


def parse_api_data(response_json, group_name: str):
    parsed_items = []

    if not isinstance(response_json, dict):
        return parsed_items

    sessions = response_json.get("data", [])
    if not isinstance(sessions, list):
        return parsed_items

    buy_url = EVENTS[group_name]["buy_url"]

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
            "• **Restock alert:** realtime melalui local collector\n"
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
                "Status ketersediaan terbaru dari local collector.",
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

        try:
            self.prev_state = load_event_state()
        except Exception:
            log.exception("Gagal membaca event_state database.")
            self.prev_state = {}

        self.restore_cached_data()

    def restore_cached_data(self):
        restored = []
        latest_by_group = {group: None for group in GROUP_NAMES}

        for uid, row in self.prev_state.items():
            try:
                group = row["group_name"]

                if group not in EVENTS:
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
                        "buy_url": EVENTS[group]["buy_url"],
                    }
                )

                updated_at = row.get("updated_at")

                if updated_at and (
                    latest_by_group[group] is None
                    or updated_at > latest_by_group[group]
                ):
                    latest_by_group[group] = updated_at

            except Exception:
                continue

        with STATE_LOCK:
            if restored:
                RUNTIME_STATE["members"] = restored

            latest = None

            for group in GROUP_NAMES:
                meta = RUNTIME_STATE["groups"][group]
                meta["status"] = "cached"
                meta["last_success"] = latest_by_group[group]
                meta["error"] = "Menunggu snapshot baru dari local collector."

                if latest_by_group[group] and (
                    latest is None or latest_by_group[group] > latest
                ):
                    latest = latest_by_group[group]

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

    def ingest_snapshot(self, payload: dict):
        if not isinstance(payload, dict):
            raise ValueError("Payload collector harus berupa JSON object.")

        groups_payload = payload.get("groups")

        if not isinstance(groups_payload, dict):
            raise ValueError("Payload harus memiliki object 'groups'.")

        received_iso = _iso_now()
        collector_time = payload.get("collector_time") or received_iso

        with STATE_LOCK:
            previous_runtime = list(RUNTIME_STATE["members"])
            RUNTIME_STATE["last_check"] = received_iso
            RUNTIME_STATE["collector_last_seen"] = received_iso

        dashboard_by_group = {
            group: [
                member
                for member in previous_runtime
                if member["group"] == group
            ]
            for group in GROUP_NAMES
        }

        result_summary = {}
        restock_count = 0

        for group in GROUP_NAMES:
            report = groups_payload.get(group)

            if not isinstance(report, dict):
                continue

            checked_at = report.get("checked_at") or collector_time
            http_status = report.get("http_status")
            ok = bool(report.get("ok"))

            with STATE_LOCK:
                meta = RUNTIME_STATE["groups"][group]
                meta["last_attempt"] = checked_at
                meta["last_http"] = http_status

            if not ok:
                error = str(report.get("error") or "Local collector gagal mengambil API.")

                with STATE_LOCK:
                    meta = RUNTIME_STATE["groups"][group]
                    meta["status"] = "cached" if dashboard_by_group[group] else "error"
                    meta["error"] = error

                result_summary[group] = {
                    "ok": False,
                    "error": error,
                }

                continue

            raw_data = report.get("data")

            parsed = parse_api_data(
                raw_data,
                group,
            )

            # Jika collector menyatakan sukses tetapi JSON tidak menghasilkan
            # data sama sekali, jangan hapus cache lama secara otomatis.
            if not parsed:
                error = "Collector mendapat response, tetapi tidak ada slot yang dapat diparse."

                with STATE_LOCK:
                    meta = RUNTIME_STATE["groups"][group]
                    meta["status"] = "cached" if dashboard_by_group[group] else "error"
                    meta["error"] = error

                result_summary[group] = {
                    "ok": False,
                    "error": error,
                }

                continue

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

                        add_restock_log(
                            item,
                            old_stock,
                            item["stock"],
                        )

                        broadcast_restock(
                            item,
                            old_stock,
                        )

                        restock_count += 1

                upsert_event_state(item)

                self.prev_state[uid] = {
                    "uid": uid,
                    "group_name": item["group"],
                    "member_name": item["name"],
                    "session_name": item["session"],
                    "track_name": item["track"],
                    "stock": item["stock"],
                    "updated_at": checked_at,
                }

            dashboard_by_group[group] = parsed

            with STATE_LOCK:
                meta = RUNTIME_STATE["groups"][group]
                meta["status"] = "live"
                meta["last_success"] = checked_at
                meta["last_http"] = http_status or 200
                meta["error"] = None
                meta["retry_in"] = 0

            result_summary[group] = {
                "ok": True,
                "slots": len(parsed),
            }

        combined = []

        for group in GROUP_NAMES:
            combined.extend(dashboard_by_group[group])

        with STATE_LOCK:
            RUNTIME_STATE["members"] = combined

            successes = [
                meta.get("last_success")
                for meta in RUNTIME_STATE["groups"].values()
                if meta.get("last_success")
            ]

            RUNTIME_STATE["last_success"] = (
                max(successes)
                if successes
                else None
            )

            errors = []

            for group, meta in RUNTIME_STATE["groups"].items():
                if meta.get("status") != "live" and meta.get("error"):
                    errors.append(
                        f"{group}: {meta['error']}"
                    )

            RUNTIME_STATE["last_error"] = (
                " | ".join(errors)
                if errors
                else None
            )

        log.info(
            "Snapshot collector diterima. result=%s restocks=%s",
            result_summary,
            restock_count,
        )

        return {
            "ok": True,
            "received_at": received_iso,
            "groups": result_summary,
            "restocks": restock_count,
        }

    def _mark_stale_if_needed(self):
        state = snapshot()
        last_seen = _parse_iso(state.get("collector_last_seen"))

        if last_seen is None:
            return

        now = datetime.now(last_seen.tzinfo or timezone.utc)

        age = (
            now - last_seen
        ).total_seconds()

        if age <= settings.collector_stale_after:
            return

        with STATE_LOCK:
            for group in GROUP_NAMES:
                meta = RUNTIME_STATE["groups"][group]

                if meta["status"] == "live":
                    meta["status"] = "cached"
                    meta["error"] = (
                        f"Local collector belum mengirim update selama "
                        f"{int(age)} detik."
                    )

            RUNTIME_STATE["last_error"] = (
                f"Local collector tidak mengirim snapshot terbaru "
                f"selama {int(age)} detik."
            )

    def run(self):
        with STATE_LOCK:
            RUNTIME_STATE["running"] = True

        log.info(
            "Monitor Deplexo aktif dalam mode LOCAL COLLECTOR."
        )

        try:
            while not self.stop_event.is_set():
                try:
                    self._mark_stale_if_needed()
                    self.maybe_send_scheduled_report()

                except Exception as exc:
                    log.exception("Monitor loop error")

                    with STATE_LOCK:
                        RUNTIME_STATE["last_error"] = (
                            f"{type(exc).__name__}: {exc}"
                        )

                self.stop_event.wait(5)

        finally:
            with STATE_LOCK:
                RUNTIME_STATE["running"] = False

    def maybe_send_scheduled_report(self):
        now = _jakarta_now()

        if now.hour not in (
            8,
            12,
            20,
        ):
            return

        if now.minute >= 5:
            return

        schedule_key = (
            f"{now.date().isoformat()}-"
            f"{now.hour}"
        )

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

        log.info(
            "Scheduled report %02d:00 WIB terkirim.",
            now.hour,
        )
