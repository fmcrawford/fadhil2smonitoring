import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from .config import settings
from .db import (
    add_restock_log,
    claim_schedule_run,
    get_active_collector_source,
    list_collector_source_states,
    list_enabled_webhooks,
    list_mng_weekly_status,
    load_event_state,
    mark_mng_extra_session_live,
    mark_mng_release_notified,
    mng_week_has_cutoff,
    save_mng_weekly_cutoff,
    set_active_collector_source,
    target_user_ids,
    update_collector_source_state,
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
            "error": "Menunggu collector hybrid.",
            "retry_in": 0,
            "source": "cloud",
            "source_reason": "default cloud priority",
            "source_switched_at": None,
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


def _seconds_since(value):
    parsed = _parse_iso(value)
    if parsed is None:
        return None

    now = datetime.now(timezone.utc)
    parsed_utc = parsed.astimezone(timezone.utc)
    return max(
        0,
        (now - parsed_utc).total_seconds(),
    )


def _collector_overview():
    rows = list_collector_source_states()
    grouped = {
        "cloud": [],
        "pc": [],
    }

    for row in rows:
        collector_id = str(
            row.get("collector_id") or ""
        ).lower()
        if collector_id in grouped:
            grouped[collector_id].append(row)

    result = {}

    for collector_id, source_rows in grouped.items():
        if not source_rows:
            result[collector_id] = {
                "status": "waiting",
                "last_seen": None,
                "last_success": None,
            }
            continue

        latest_seen = max(
            (
                row.get("last_seen")
                for row in source_rows
                if row.get("last_seen")
            ),
            default=None,
        )
        latest_success = max(
            (
                row.get("last_success")
                for row in source_rows
                if row.get("last_success")
            ),
            default=None,
        )

        seen_age = _seconds_since(latest_seen)

        if (
            seen_age is None
            or seen_age > settings.hybrid_source_offline_after
        ):
            status = "offline"
        elif any(
            int(row.get("consecutive_failures", 0) or 0) > 0
            for row in source_rows
        ):
            status = "degraded"
        else:
            status = "connected"

        result[collector_id] = {
            "status": status,
            "last_seen": latest_seen,
            "last_success": latest_success,
        }

    return result


def snapshot():
    with STATE_LOCK:
        state = {
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

    state["collectors"] = _collector_overview()
    return state


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


def _scheduled_group_live(group_meta, group_name):
    return group_meta.get(group_name, {}).get("status") == "live"


def send_scheduled_report(members, report_hour, group_meta=None):
    hooks = list_enabled_webhooks()
    if not hooks:
        return 0

    group_meta = group_meta or {}
    jkt_text = available_group_text(members, "JKT48")
    akb_text = available_group_text(members, "AKB48")
    mng_highlight_text = available_mng_highlights_text(members)
    sent_messages = 0

    for hook in hooks:
        try:
            webhook_url = decrypt_webhook(hook["webhook_url_enc"])
            shot_fields = []
            sent_groups = []

            if hook["notify_jkt"] and _scheduled_group_live(group_meta, "JKT48"):
                shot_fields.append(
                    {
                        "name": "🏢 JKT48 2-Shot",
                        "value": jkt_text,
                        "inline": False,
                    }
                )
                sent_groups.append("JKT48")

            if hook["notify_akb"] and _scheduled_group_live(group_meta, "AKB48"):
                shot_fields.append(
                    {
                        "name": "🏢 AKB48 2-Shot",
                        "value": akb_text,
                        "inline": False,
                    }
                )
                sent_groups.append("AKB48")

            if shot_fields:
                send_embed(
                    webhook_url,
                    f"📸 2-SHOT SUMMARY • {report_hour:02d}:00 WIB",
                    "Ringkasan slot 2-Shot yang masih tersedia.",
                    COLOR_PURPLE,
                    fields=shot_fields,
                )
                sent_messages += 1

            if (
                hook["notify_mng"]
                and _scheduled_group_live(group_meta, MNG_GROUP)
            ):
                send_embed(
                    webhook_url,
                    f"🤝 M&G HIGHLIGHT • {report_hour:02d}:00 WIB",
                    (
                        "Hanya menampilkan **member pantauan yang masih tersedia**.\n\n"
                        f"{mng_highlight_text}"
                    ),
                    COLOR_ORANGE,
                )
                sent_messages += 1
                sent_groups.append(MNG_GROUP)

            if sent_groups:
                log.info(
                    "Scheduled report webhook id=%s name=%s groups=%s",
                    hook["id"],
                    hook["name"],
                    ",".join(sent_groups),
                )

        except Exception:
            log.exception("Scheduled report gagal webhook id=%s", hook["id"])

    return sent_messages


class MonitorService:
    def __init__(self):
        self.stop_event = threading.Event()
        self.thread = None
        self.last_schedule_key = None
        self.ingest_lock = threading.Lock()
        self.source_cache = {
            "cloud": {
                group: None
                for group in GROUP_NAMES
            },
            "pc": {
                group: None
                for group in GROUP_NAMES
            },
        }

        try:
            self.prev_state = load_event_state()
        except Exception:
            log.exception("Gagal membaca event_state database.")
            self.prev_state = {}

        self.restore_cached_data()

    def restore_cached_data(self):
        restored = []
        latest_by_group = {
            group: None
            for group in GROUP_NAMES
        }

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

        active_rows = {
            group: get_active_collector_source(group)
            for group in GROUP_NAMES
        }
        source_rows = {
            group: {
                row["collector_id"]: row
                for row in list_collector_source_states(group)
            }
            for group in GROUP_NAMES
        }

        now_utc = datetime.now(timezone.utc)

        with STATE_LOCK:
            if restored:
                RUNTIME_STATE["members"] = restored

            latest = None
            stale_groups = []

            for group in GROUP_NAMES:
                meta = RUNTIME_STATE["groups"][group]
                latest_group = latest_by_group[group]
                active_row = active_rows[group]
                active_source = active_row["collector_id"]
                active_health = source_rows[group].get(
                    active_source,
                    {},
                )

                meta["source"] = active_source
                meta["source_reason"] = active_row.get("reason")
                meta["source_switched_at"] = active_row.get(
                    "switched_at"
                )
                meta["last_success"] = latest_group
                meta["last_attempt"] = latest_group
                meta["retry_in"] = 0
                meta["last_http"] = active_health.get("last_http")

                parsed = _parse_iso(latest_group)

                if parsed is not None:
                    parsed_utc = parsed.astimezone(timezone.utc)
                    age = max(
                        0,
                        (now_utc - parsed_utc).total_seconds(),
                    )

                    if age <= settings.collector_stale_after:
                        meta["status"] = "live"
                        meta["last_http"] = (
                            meta["last_http"]
                            if meta["last_http"] is not None
                            else 200
                        )
                        meta["error"] = None
                    else:
                        meta["status"] = "cached"
                        meta["error"] = (
                            "Snapshot authoritative terakhir berumur "
                            f"{int(age)} detik."
                        )
                        stale_groups.append(group)
                else:
                    meta["status"] = "cached"
                    meta["error"] = (
                        "Menunggu snapshot collector hybrid."
                    )
                    stale_groups.append(group)

                if latest_group and (
                    latest is None
                    or latest_group > latest
                ):
                    latest = latest_group

            RUNTIME_STATE["last_success"] = latest
            RUNTIME_STATE["last_check"] = latest
            RUNTIME_STATE["collector_last_seen"] = latest

            if stale_groups:
                RUNTIME_STATE["last_error"] = (
                    "Data belum fresh untuk: "
                    + ", ".join(stale_groups)
                )
            else:
                RUNTIME_STATE["last_error"] = None

        if restored:
            log.info(
                "Hybrid state dipulihkan: %s slot.",
                len(restored),
            )

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

    def _source_fresh(self, row):
        if not row:
            return False

        age = _seconds_since(
            row.get("last_seen")
        )

        return (
            age is not None
            and age <= settings.hybrid_source_offline_after
        )

    def _source_good(self, row):
        if not self._source_fresh(row):
            return False

        success_age = _seconds_since(
            row.get("last_success")
        )

        return (
            int(row.get("last_http") or 0) == 200
            and success_age is not None
            and success_age <= settings.collector_stale_after
        )

    def _choose_active_source(self, group):
        active_row = get_active_collector_source(group)
        active = active_row["collector_id"]

        states = {
            row["collector_id"]: row
            for row in list_collector_source_states(group)
        }

        cloud = states.get("cloud")
        pc = states.get("pc")

        changed = False
        reason = active_row.get("reason") or ""

        if active == "cloud":
            cloud_failures = int(
                (cloud or {}).get(
                    "consecutive_failures",
                    0,
                )
                or 0
            )

            cloud_unhealthy = (
                not self._source_fresh(cloud)
                or cloud_failures
                >= settings.hybrid_failover_failures
            )

            if (
                cloud_unhealthy
                and self._source_good(pc)
            ):
                active = "pc"
                changed = True
                reason = (
                    "cloud gagal/offline; "
                    "PC fallback sehat"
                )

        elif active == "pc":
            pc_failures = int(
                (pc or {}).get(
                    "consecutive_failures",
                    0,
                )
                or 0
            )

            pc_unhealthy = (
                not self._source_fresh(pc)
                or pc_failures
                >= settings.hybrid_failover_failures
            )

            cloud_recovered = (
                self._source_good(cloud)
                and int(
                    (cloud or {}).get(
                        "consecutive_successes",
                        0,
                    )
                    or 0
                )
                >= settings.hybrid_cloud_recovery_successes
            )

            if (
                pc_unhealthy
                and self._source_good(cloud)
            ):
                active = "cloud"
                changed = True
                reason = (
                    "PC fallback gagal/offline; "
                    "kembali ke cloud"
                )
            elif cloud_recovered:
                active = "cloud"
                changed = True
                reason = (
                    "cloud pulih stabil; "
                    "failback ke cloud"
                )

        else:
            if self._source_good(cloud):
                active = "cloud"
            elif self._source_good(pc):
                active = "pc"
            else:
                active = "cloud"

            changed = True
            reason = "normalisasi active source"

        if changed:
            active_row = set_active_collector_source(
                group,
                active,
                reason,
            )

            log.warning(
                "HYBRID SWITCH %s -> %s | %s",
                group,
                active,
                reason,
            )

        return active, changed, active_row

    def _apply_group_data(
        self,
        group,
        parsed,
        checked_at,
        source,
        source_reason=None,
        source_switched_at=None,
    ):
        restock_count = 0

        for item in parsed:
            uid = item["id"]
            previous = self.prev_state.get(uid)

            if previous is not None:
                try:
                    old_stock = int(
                        previous.get("stock", 0)
                    )
                except Exception:
                    old_stock = 0

                if (
                    old_stock <= 0
                    and item["stock"] > 0
                ):
                    log.warning(
                        "RESTOCK %s | %s | %s | %s | %s -> %s | source=%s",
                        item["group"],
                        item["name"],
                        item["session"],
                        item["track"],
                        old_stock,
                        item["stock"],
                        source,
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

        with STATE_LOCK:
            others = [
                member
                for member in RUNTIME_STATE["members"]
                if member["group"] != group
            ]
            RUNTIME_STATE["members"] = (
                others
                + list(parsed)
            )

            meta = RUNTIME_STATE["groups"][group]
            meta["status"] = "live"
            meta["last_attempt"] = checked_at
            meta["last_success"] = checked_at
            meta["last_http"] = 200
            meta["error"] = None
            meta["retry_in"] = 0
            meta["source"] = source

            if source_reason is not None:
                meta["source_reason"] = source_reason

            if source_switched_at is not None:
                meta["source_switched_at"] = source_switched_at

        return restock_count

    def _mark_primary_failure(
        self,
        group,
        source,
        checked_at,
        http_status,
        error,
        failures,
    ):
        with STATE_LOCK:
            has_data = any(
                member["group"] == group
                for member in RUNTIME_STATE["members"]
            )

            meta = RUNTIME_STATE["groups"][group]
            meta["last_attempt"] = checked_at
            meta["last_http"] = http_status
            meta["error"] = error
            meta["source"] = source

            if not has_data:
                meta["status"] = "error"
            elif failures < settings.hybrid_failover_failures:
                meta["status"] = "retrying"
            else:
                meta["status"] = "cached"

    def ingest_snapshot(self, payload: dict):
        if not isinstance(payload, dict):
            raise ValueError(
                "Payload collector harus berupa JSON object."
            )

        collector_id = str(
            payload.get("collector_id")
            or "cloud"
        ).strip().lower()

        if collector_id not in ("cloud", "pc"):
            raise ValueError(
                "collector_id harus 'cloud' atau 'pc'."
            )

        groups_payload = payload.get("groups")
        if not isinstance(groups_payload, dict):
            raise ValueError(
                "Payload harus memiliki object 'groups'."
            )

        received_iso = _iso_now()
        collector_time = (
            payload.get("collector_time")
            or received_iso
        )

        with self.ingest_lock:
            with STATE_LOCK:
                RUNTIME_STATE["last_check"] = received_iso
                RUNTIME_STATE["collector_last_seen"] = received_iso

            result_summary = {}
            restock_count = 0

            for group in GROUP_NAMES:
                report = groups_payload.get(group)

                if not isinstance(report, dict):
                    continue

                checked_at = (
                    report.get("checked_at")
                    or collector_time
                )
                http_status = report.get("http_status")
                ok = bool(report.get("ok"))
                error = None
                parsed = None

                if ok:
                    parsed = parse_api_data(
                        report.get("data"),
                        group,
                    )

                    if not parsed:
                        ok = False
                        error = (
                            "Collector mendapat response, "
                            "tetapi tidak ada slot yang dapat diparse."
                        )
                else:
                    error = str(
                        report.get("error")
                        or "Collector gagal mengambil API."
                    )

                source_state = update_collector_source_state(
                    collector_id,
                    group,
                    ok,
                    http_status,
                    error,
                )

                if ok:
                    self.source_cache[
                        collector_id
                    ][group] = {
                        "parsed": parsed,
                        "checked_at": checked_at,
                        "received_at": received_iso,
                    }

                (
                    active_source,
                    switched,
                    active_row,
                ) = self._choose_active_source(group)

                applied = False

                if (
                    switched
                    and active_source != collector_id
                ):
                    cached = self.source_cache[
                        active_source
                    ].get(group)

                    if (
                        cached
                        and (
                            _seconds_since(
                                cached.get("received_at")
                            )
                            or 0
                        )
                        <= settings.hybrid_source_offline_after
                    ):
                        restock_count += self._apply_group_data(
                            group,
                            cached["parsed"],
                            cached["checked_at"],
                            active_source,
                            active_row.get("reason"),
                            active_row.get("switched_at"),
                        )
                        applied = True
                    else:
                        with STATE_LOCK:
                            meta = RUNTIME_STATE["groups"][group]
                            meta["source"] = active_source
                            meta["source_reason"] = active_row.get(
                                "reason"
                            )
                            meta["source_switched_at"] = active_row.get(
                                "switched_at"
                            )
                            if meta.get("status") == "live":
                                meta["status"] = "retrying"

                if active_source == collector_id:
                    if ok:
                        restock_count += self._apply_group_data(
                            group,
                            parsed,
                            checked_at,
                            collector_id,
                            active_row.get("reason"),
                            active_row.get("switched_at"),
                        )
                        applied = True
                    else:
                        self._mark_primary_failure(
                            group,
                            collector_id,
                            checked_at,
                            http_status,
                            error,
                            int(
                                source_state.get(
                                    "consecutive_failures",
                                    0,
                                )
                                or 0
                            ),
                        )

                result_summary[group] = {
                    "ok": ok,
                    "source": collector_id,
                    "active_source": active_source,
                    "applied": applied,
                    "http_status": http_status,
                }

                if error:
                    result_summary[group]["error"] = error

            with STATE_LOCK:
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

                for group, meta in (
                    RUNTIME_STATE["groups"].items()
                ):
                    if (
                        meta.get("status")
                        in ("cached", "error")
                        and meta.get("error")
                    ):
                        errors.append(
                            f"{group}: {meta['error']}"
                        )

                RUNTIME_STATE["last_error"] = (
                    " | ".join(errors)
                    if errors
                    else None
                )

            log.info(
                "Hybrid snapshot source=%s result=%s restocks=%s",
                collector_id,
                result_summary,
                restock_count,
            )

            return {
                "ok": True,
                "collector_id": collector_id,
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
                        "Tidak ada snapshot dari cloud maupun PC "
                        f"selama {int(age)} detik."
                    )

            RUNTIME_STATE["last_error"] = (
                "Hybrid collector tidak mengirim snapshot terbaru "
                f"selama {int(age)} detik."
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

        schedule_key = (
            f"scheduled-report:{now.date().isoformat()}:{now.hour:02d}"
        )
        if self.last_schedule_key == schedule_key:
            return

        state = snapshot()
        if not state["members"]:
            return

        live_groups = [
            group
            for group in GROUP_NAMES
            if state["groups"].get(group, {}).get("status") == "live"
        ]

        if not live_groups:
            log.info(
                "Scheduled report %02d:00 WIB ditunda: tidak ada group LIVE.",
                now.hour,
            )
            return

        if not claim_schedule_run(schedule_key):
            self.last_schedule_key = schedule_key
            log.info(
                "Scheduled report %02d:00 WIB sudah pernah diproses.",
                now.hour,
            )
            return

        sent_messages = send_scheduled_report(
            state["members"],
            now.hour,
            group_meta=state["groups"],
        )
        self.last_schedule_key = schedule_key
        log.info(
            "Scheduled report %02d:00 WIB selesai. messages=%s live_groups=%s",
            now.hour,
            sent_messages,
            ",".join(live_groups),
        )
