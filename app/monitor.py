import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from .config import settings
from .db import (
    add_restock_log,
    list_enabled_webhooks,
    list_mng_weekly_status,
    load_event_state,
    mark_mng_extra_session_live,
    mark_mng_release_notified,
    mng_week_has_cutoff,
    save_mng_weekly_cutoff,
    target_user_ids,
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
    "JKT48_MNG": {
        "buy_url": "https://jkt48.com/purchase/exclusive?code=EX24AE",
    },
}

GROUP_NAMES = ["JKT48", "AKB48", "JKT48_MNG"]

COLOR_GREEN = 0x2ECC71
COLOR_BLUE = 0x3498DB
COLOR_PURPLE = 0x9B59B6
COLOR_GOLD = 0xF1C40F
COLOR_ORANGE = 0xE67E22

MNG_GROUP = "JKT48_MNG"
MNG_MAX_SESSIONS = 4
MNG_CUTOFF_HOUR = 12
MNG_CUTOFF_WINDOW_MINUTES = 10
MNG_RELEASE_HOUR = 19

MNG_HIGHLIGHT_MEMBERS = [
    "Grace Octaviani",
    "Michelle Alexandra",
    "Jazzlyn Trisha",
    "Fiony Alveria",
    "Indah Cahya",
    "Marsha Lenathea",
    "Nina Tutachia",
    "Fritzy Rosmerian",
    "Aurhel Alana",
]

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
            "consecutive_failures": 0,
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



def mng_week_key(now=None):
    now = now or _jakarta_now()
    days_since_sunday = (now.weekday() + 1) % 7
    sunday = now.date() - timedelta(days=days_since_sunday)
    return sunday.isoformat()


def _mng_member_rollup(members):
    rollup = {}
    for item in members:
        if item.get("group") != MNG_GROUP:
            continue
        name = item.get("name")
        if not name:
            continue
        row = rollup.setdefault(
            name,
            {
                "member_name": name,
                "sessions": set(),
                "total_stock": 0,
                "available_slots": 0,
            },
        )
        row["sessions"].add(str(item.get("session") or "-"))
        stock = int(item.get("stock", 0) or 0)
        row["total_stock"] += stock
        if stock > 0:
            row["available_slots"] += 1
    return rollup


def build_mng_weekly_board(members, qualification_rows):
    current = _mng_member_rollup(members)
    saved = {row["member_name"]: dict(row) for row in qualification_rows}
    names = sorted(set(current) | set(saved), key=str.casefold)
    board = []

    for name in names:
        live = current.get(
            name,
            {
                "member_name": name,
                "sessions": set(),
                "total_stock": 0,
                "available_slots": 0,
            },
        )
        q = saved.get(name)
        session_count = len(live["sessions"])
        total_stock = int(live["total_stock"])
        all_sold_out = session_count > 0 and total_stock <= 0

        status = "TRACKING"
        status_label = "Tracking"
        priority = 5

        if q:
            if q.get("released_at"):
                status = "EXTRA_LIVE"
                status_label = "Extra Session Live"
                priority = 0
            elif int(q.get("eligible", 0)):
                status = "ELIGIBLE"
                status_label = "Eligible • Menunggu Senin 19:00"
                priority = 1
            elif int(q.get("maxed", 0)):
                status = "MAXED"
                status_label = "Max 4 Sesi • Tidak Ada Tambahan"
                priority = 2
            else:
                status = "NOT_QUALIFIED"
                status_label = "Tidak Qualified pada Cutoff"
                priority = 4
        elif all_sold_out and session_count >= MNG_MAX_SESSIONS:
            status = "MAXED_PREVIEW"
            status_label = "4/4 Sold Out • Maksimum"
            priority = 2
        elif all_sold_out:
            status = "SOLD_OUT_WAITING"
            status_label = "Sold Out • Menunggu Cutoff Minggu"
            priority = 3
        elif total_stock > 0:
            status = "AVAILABLE"
            status_label = "Tiket Masih Tersedia"
            priority = 4

        released_sessions = []
        if q and q.get("released_session_names"):
            try:
                released_sessions = json.loads(q["released_session_names"])
            except Exception:
                released_sessions = []

        board.append(
            {
                "member_name": name,
                "session_count": session_count,
                "total_stock": total_stock,
                "available_slots": int(live["available_slots"]),
                "all_sold_out": all_sold_out,
                "status": status,
                "status_label": status_label,
                "priority": priority,
                "eligible": bool(q and int(q.get("eligible", 0))),
                "maxed": bool(q and int(q.get("maxed", 0))),
                "released_at": q.get("released_at") if q else None,
                "released_sessions": released_sessions,
                "buy_url": EVENTS[MNG_GROUP]["buy_url"],
            }
        )

    return sorted(board, key=lambda x: (x["priority"], x["member_name"].casefold()))


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
            "• **Member Sniping:** target dapat dipilih dari dashboard\n"
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

    is_mng = item["group"] == "JKT48_MNG"
    product_label = "M&G" if is_mng else "2-SHOT"
    display_group = "JKT48 M&G" if is_mng else item["group"]

    if not hooks:
        log.info(
            "Restock %s %s terdeteksi, tetapi tidak ada webhook aktif.",
            item["group"],
            item["name"],
        )
        return

    sniping_users = target_user_ids(item["group"], item["name"])

    base_description = (
        f"> 🏢 **Event:** `{display_group}`\n"
        f"> 👤 **Member:** `{item['name']}`\n"
        f"> 🕒 **Sesi:** `{item['session']}`\n"
        f"> 📍 **Jalur:** `{item['track']}`\n"
        f"> 📦 **Stok:** `{old_stock} → {item['stock']}`\n\n"
        f"👉 **[BELI TIKET {product_label}]({item['buy_url']})**"
    )

    for hook in hooks:
        try:
            webhook_url = decrypt_webhook(hook["webhook_url_enc"])
            is_target = int(hook["user_id"]) in sniping_users

            if is_target:
                title = "🎯 SNIPING TARGET RESTOCK!"
                description = (
                    "⭐ **Member ini ada di daftar sniping Anda.**\n\n"
                    + base_description
                )
                content = (
                    "@everyone 🎯 **TARGET MEMBER RESTOCK!**"
                    if hook["mention_everyone"]
                    else "🎯 **TARGET MEMBER RESTOCK!**"
                )
                color = COLOR_GOLD
            else:
                title = f"🚨 {product_label} RESTOCK ALERT!"
                description = base_description
                content = (
                    "@everyone 🚨 **RESTOCK TERDETEKSI!**"
                    if hook["mention_everyone"]
                    else "🚨 **RESTOCK TERDETEKSI!**"
                )
                color = COLOR_BLUE

            send_embed(
                webhook_url,
                title,
                description,
                color,
                content=content,
            )

            log.info(
                "Restock dikirim ke webhook id=%s target=%s",
                hook["id"],
                is_target,
            )

        except Exception:
            log.exception("Gagal mengirim restock webhook id=%s", hook["id"])



def broadcast_mng_cutoff(rows, week_key: str):
    hooks = list_enabled_webhooks(MNG_GROUP)
    if not hooks:
        return

    eligible = [r["member_name"] for r in rows if r["eligible"]]
    maxed = [r["member_name"] for r in rows if r["maxed"]]
    eligible_text = "\n".join(f"⭐ **{n}**" for n in eligible) if eligible else "Belum ada member yang qualified."
    maxed_text = "\n".join(f"🔒 **{n}**" for n in maxed) if maxed else "Tidak ada."

    for hook in hooks:
        try:
            webhook_url = decrypt_webhook(hook["webhook_url_enc"])
            content = (
                "@everyone 🤝 **M&G WEEKLY CUTOFF!**"
                if hook["mention_everyone"]
                else "🤝 **M&G WEEKLY CUTOFF!**"
            )
            send_embed(
                webhook_url,
                "🤝 JKT48 M&G • HASIL CUTOFF MINGGU 12:00",
                (
                    f"Weekly cycle `{week_key}` telah dikunci.\n\n"
                    "**Berhak sesi tambahan (sesi < 4 dan seluruh tiket SO):**\n"
                    f"{eligible_text}\n\n"
                    "**Sudah maksimum 4 sesi:**\n"
                    f"{maxed_text}\n\n"
                    "Sesi tambahan member qualified dipantau pada Senin 19:00 WIB.\n\n"
                    + purchase_link_text(MNG_GROUP, "BUKA HALAMAN JKT48 M&G")
                ),
                COLOR_ORANGE,
                content=content,
            )
        except Exception:
            log.exception("Gagal mengirim M&G cutoff webhook id=%s", hook["id"])


def broadcast_mng_extra_session(member_name: str, new_sessions: list[str]):
    hooks = list_enabled_webhooks(MNG_GROUP)
    if not hooks:
        return

    sniping_users = target_user_ids(MNG_GROUP, member_name)
    session_text = "\n".join(f"• `{name}`" for name in new_sessions) or "• Sesi baru"
    buy_url = EVENTS[MNG_GROUP]["buy_url"]

    for hook in hooks:
        try:
            webhook_url = decrypt_webhook(hook["webhook_url_enc"])
            is_target = int(hook["user_id"]) in sniping_users
            title = (
                "🎯 M&G TARGET • EXTRA SESSION LIVE!"
                if is_target
                else "🤝 M&G EXTRA SESSION LIVE!"
            )
            content = (
                "@everyone 🚨 **SESI TAMBAHAN M&G SUDAH LIVE!**"
                if hook["mention_everyone"]
                else "🚨 **SESI TAMBAHAN M&G SUDAH LIVE!**"
            )
            send_embed(
                webhook_url,
                title,
                (
                    f"👤 **{member_name}**\n\n"
                    f"**Sesi baru terdeteksi:**\n{session_text}\n\n"
                    f"👉 **[BELI M&G SEKARANG]({buy_url})**"
                ),
                COLOR_GOLD,
                content=content,
            )
        except Exception:
            log.exception("Gagal mengirim M&G release webhook id=%s", hook["id"])


def purchase_link_text(group_name: str, label=None):
    url = EVENTS.get(group_name, {}).get("buy_url")
    if not url:
        return ""

    if label is None:
        if group_name == "JKT48":
            label = "BELI JKT48 2-SHOT"
        elif group_name == "AKB48":
            label = "BELI AKB48 2-SHOT"
        elif group_name == MNG_GROUP:
            label = "BELI JKT48 M&G"
        else:
            label = "BUKA HALAMAN PEMBELIAN"

    return f"👉 **[{label}]({url})**"


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

    link = purchase_link_text(group_name)
    if link:
        lines.extend(["", link])

    return "\n".join(lines)[:1000]



def available_mng_highlights_text(members):
    wanted = {name.casefold(): name for name in MNG_HIGHLIGHT_MEMBERS}
    grouped = {}

    for item in members:
        if item.get("group") != MNG_GROUP:
            continue

        name = str(item.get("name") or "").strip()
        key = name.casefold()
        if key not in wanted:
            continue

        stock = int(item.get("stock", 0) or 0)
        if stock <= 0:
            continue

        row = grouped.setdefault(
            wanted[key],
            {
                "total_stock": 0,
                "slots": [],
            },
        )
        row["total_stock"] += stock
        row["slots"].append(
            {
                "session": str(item.get("session") or "-"),
                "track": str(item.get("track") or "-"),
                "stock": stock,
            }
        )

    if not grouped:
        return (
            "Tidak ada member pantauan yang sedang tersedia. "
            "Semua target highlight sedang sold out / belum tersedia.\n\n"
            + purchase_link_text(MNG_GROUP, "BUKA HALAMAN JKT48 M&G")
        )

    lines = []
    for name in MNG_HIGHLIGHT_MEMBERS:
        row = grouped.get(name)
        if not row:
            continue

        lines.append(f"🎟️ **{name}** — **{row['total_stock']} tiket**")

        for slot in row["slots"][:2]:
            lines.append(
                f"↳ `{slot['session']}` • `{slot['track']}` "
                f"→ **{slot['stock']}**"
            )

        remaining = len(row["slots"]) - 2
        if remaining > 0:
            lines.append(f"↳ +{remaining} slot tersedia lainnya")

    link = purchase_link_text(MNG_GROUP, "BELI JKT48 M&G")
    if link:
        lines.extend(["", link])

    return "\n".join(lines)[:3800]


def send_scheduled_report(members, report_hour, group_meta=None):
    hooks = list_enabled_webhooks()

    if not hooks:
        return

    group_meta = group_meta or {}

    jkt_text = available_group_text(members, "JKT48")
    akb_text = available_group_text(members, "AKB48")
    mng_highlight_text = available_mng_highlights_text(members)

    for hook in hooks:
        try:
            webhook_url = decrypt_webhook(hook["webhook_url_enc"])

            shot_fields = []

            if hook["notify_jkt"]:
                status = group_meta.get("JKT48", {}).get("status", "cached")
                suffix = "" if status == "live" else " ⚠️ cached"
                shot_fields.append(
                    {
                        "name": f"🏢 JKT48 2-Shot{suffix}",
                        "value": jkt_text,
                        "inline": False,
                    }
                )

            if hook["notify_akb"]:
                status = group_meta.get("AKB48", {}).get("status", "cached")
                suffix = "" if status == "live" else " ⚠️ cached"
                shot_fields.append(
                    {
                        "name": f"🏢 AKB48 2-Shot{suffix}",
                        "value": akb_text,
                        "inline": False,
                    }
                )

            if shot_fields:
                send_embed(
                    webhook_url,
                    f"📸 2-SHOT SUMMARY • {report_hour:02d}:00 WIB",
                    "Ringkasan slot 2-Shot yang masih tersedia.",
                    COLOR_PURPLE,
                    fields=shot_fields,
                )

            if hook["notify_mng"]:
                status = group_meta.get(MNG_GROUP, {}).get("status", "cached")
                suffix = "" if status == "live" else " ⚠️ cached"

                send_embed(
                    webhook_url,
                    f"🤝 M&G HIGHLIGHT • {report_hour:02d}:00 WIB{suffix}",
                    (
                        "Hanya menampilkan **member pantauan yang masih tersedia**.\n\n"
                        f"{mng_highlight_text}"
                    ),
                    COLOR_ORANGE,
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
                error = str(
                    report.get("error")
                    or "Local collector gagal mengambil API."
                )
                retrying = bool(
                    report.get("retrying")
                )
                retry_in = int(
                    report.get("retry_in")
                    or 0
                )

                with STATE_LOCK:
                    meta = RUNTIME_STATE["groups"][group]

                    # Siklus cooldown tidak menambah strike baru.
                    if not retrying:
                        meta["consecutive_failures"] = (
                            int(
                                meta.get(
                                    "consecutive_failures",
                                    0,
                                )
                            )
                            + 1
                        )

                    failures = int(
                        meta.get(
                            "consecutive_failures",
                            0,
                        )
                    )

                    if dashboard_by_group[group]:
                        meta["status"] = (
                            "retrying"
                            if failures < 3
                            else "cached"
                        )
                    else:
                        meta["status"] = "error"

                    meta["error"] = error
                    meta["retry_in"] = retry_in

                result_summary[group] = {
                    "ok": False,
                    "error": error,
                    "retrying": (
                        failures < 3
                        and bool(
                            dashboard_by_group[group]
                        )
                    ),
                    "retry_in": retry_in,
                }
                continue

            parsed = parse_api_data(report.get("data"), group)
            if not parsed:
                error = (
                    "Collector mendapat response, tetapi tidak ada slot yang dapat diparse."
                )
                with STATE_LOCK:
                    meta = RUNTIME_STATE["groups"][group]
                    meta["status"] = "cached" if dashboard_by_group[group] else "error"
                    meta["error"] = error

                result_summary[group] = {"ok": False, "error": error}
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
                        add_restock_log(item, old_stock, item["stock"])
                        broadcast_restock(item, old_stock)
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
                meta["consecutive_failures"] = 0

            result_summary[group] = {"ok": True, "slots": len(parsed)}

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
            RUNTIME_STATE["last_success"] = max(successes) if successes else None

            errors = []
            for group, meta in RUNTIME_STATE["groups"].items():
                if meta.get("status") != "live" and meta.get("error"):
                    errors.append(f"{group}: {meta['error']}")

            RUNTIME_STATE["last_error"] = " | ".join(errors) if errors else None

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

    def maybe_capture_mng_sunday_cutoff(self):
        now = _jakarta_now()
        if now.weekday() != 6 or now.hour != MNG_CUTOFF_HOUR:
            return
        if now.minute >= MNG_CUTOFF_WINDOW_MINUTES:
            return

        state = snapshot()
        meta = state["groups"].get(MNG_GROUP, {})
        if meta.get("status") != "live":
            return

        week_key = mng_week_key(now)
        if mng_week_has_cutoff(week_key):
            return

        rollup = _mng_member_rollup(state["members"])
        if not rollup:
            return

        rows = []
        for member_name, info in rollup.items():
            sessions = sorted(info["sessions"])
            session_count = len(sessions)
            total_stock = int(info["total_stock"])
            all_sold_out = session_count > 0 and total_stock <= 0
            maxed = all_sold_out and session_count >= MNG_MAX_SESSIONS
            eligible = all_sold_out and session_count < MNG_MAX_SESSIONS
            rows.append(
                {
                    "member_name": member_name,
                    "session_count": session_count,
                    "session_names_json": json.dumps(sessions, ensure_ascii=False),
                    "total_stock": total_stock,
                    "eligible": eligible,
                    "maxed": maxed,
                }
            )

        inserted = save_mng_weekly_cutoff(week_key, now.isoformat(), rows)
        if inserted:
            log.warning("M&G Sunday cutoff tersimpan week=%s members=%s", week_key, inserted)
            broadcast_mng_cutoff(rows, week_key)

    def maybe_detect_mng_monday_release(self):
        now = _jakarta_now()
        if now.weekday() != 0 or now.hour < MNG_RELEASE_HOUR:
            return

        state = snapshot()
        meta = state["groups"].get(MNG_GROUP, {})
        if meta.get("status") != "live":
            return

        week_key = mng_week_key(now)
        rows = [dict(row) for row in list_mng_weekly_status(week_key)]
        eligible = [
            row for row in rows
            if int(row.get("eligible", 0)) and not row.get("released_at")
        ]
        if not eligible:
            return

        live = _mng_member_rollup(state["members"])
        for row in eligible:
            member_name = row["member_name"]
            current = live.get(member_name)
            if not current:
                continue

            try:
                baseline = set(json.loads(row["baseline_session_names"]))
            except Exception:
                baseline = set()

            new_sessions = sorted(set(current["sessions"]) - baseline)
            if not new_sessions:
                continue

            released = mark_mng_extra_session_live(
                week_key,
                member_name,
                now.isoformat(),
                json.dumps(new_sessions, ensure_ascii=False),
            )
            if not released:
                continue

            log.warning("M&G EXTRA SESSION LIVE %s | %s", member_name, new_sessions)
            broadcast_mng_extra_session(member_name, new_sessions)
            mark_mng_release_notified(week_key, member_name)

    def _mark_stale_if_needed(self):
        state = snapshot()
        last_seen = _parse_iso(state.get("collector_last_seen"))
        if last_seen is None:
            return

        now = datetime.now(last_seen.tzinfo or timezone.utc)
        age = (now - last_seen).total_seconds()

        if age <= settings.collector_stale_after:
            return

        with STATE_LOCK:
            for group in GROUP_NAMES:
                meta = RUNTIME_STATE["groups"][group]
                if meta["status"] in ("live", "retrying"):
                    meta["status"] = "cached"
                    meta["error"] = (
                        f"Local collector belum mengirim update selama {int(age)} detik."
                    )

            RUNTIME_STATE["last_error"] = (
                f"Local collector tidak mengirim snapshot terbaru selama {int(age)} detik."
            )

    def run(self):
        with STATE_LOCK:
            RUNTIME_STATE["running"] = True

        log.info("Monitor Deplexo aktif dalam mode LOCAL COLLECTOR.")

        try:
            while not self.stop_event.is_set():
                try:
                    self._mark_stale_if_needed()
                    self.maybe_capture_mng_sunday_cutoff()
                    self.maybe_detect_mng_monday_release()
                    self.maybe_send_scheduled_report()
                except Exception as exc:
                    log.exception("Monitor loop error")
                    with STATE_LOCK:
                        RUNTIME_STATE["last_error"] = f"{type(exc).__name__}: {exc}"

                self.stop_event.wait(5)
        finally:
            with STATE_LOCK:
                RUNTIME_STATE["running"] = False

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
