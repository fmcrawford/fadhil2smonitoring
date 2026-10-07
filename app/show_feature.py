import hmac
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from fastapi import APIRouter, Body, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .config import settings
from .db import claim_schedule_run, get_user, list_user_webhooks
from .security import decrypt_webhook, read_session_token


log = logging.getLogger("48group.show")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
router = APIRouter()

COLOR_PINK = 0xFF69B4
_SERVICE = None


def _connect():
    parent = os.path.dirname(settings.database_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    conn = sqlite3.connect(settings.database_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


def init_show_db():
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS show_schedule (
                reference_code TEXT PRIMARY KEY,
                schedule_id TEXT,
                title TEXT NOT NULL,
                show_date TEXT NOT NULL,
                start_time TEXT,
                end_time TEXT,
                member_type TEXT,
                members_json TEXT NOT NULL,
                show_url TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_show_schedule_date
            ON show_schedule(show_date);

            CREATE TABLE IF NOT EXISTS show_oshi_targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                member_name TEXT NOT NULL COLLATE NOCASE,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, member_name)
            );

            CREATE INDEX IF NOT EXISTS idx_show_oshi_user
            ON show_oshi_targets(user_id);

            CREATE TABLE IF NOT EXISTS show_collector_state (
                id INTEGER PRIMARY KEY CHECK(id=1),
                last_seen TEXT,
                last_success TEXT,
                last_error TEXT,
                show_count INTEGER NOT NULL DEFAULT 0
            );

            INSERT OR IGNORE INTO show_collector_state(
                id, show_count
            ) VALUES (1, 0);
            """
        )


def _replace_show_window(window_start, window_end, shows):
    with _connect() as conn:
        conn.execute(
            """
            DELETE FROM show_schedule
            WHERE show_date BETWEEN ? AND ?
            """,
            (window_start, window_end),
        )

        for show in shows:
            conn.execute(
                """
                INSERT INTO show_schedule(
                    reference_code,
                    schedule_id,
                    title,
                    show_date,
                    start_time,
                    end_time,
                    member_type,
                    members_json,
                    show_url,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(reference_code) DO UPDATE SET
                    schedule_id=excluded.schedule_id,
                    title=excluded.title,
                    show_date=excluded.show_date,
                    start_time=excluded.start_time,
                    end_time=excluded.end_time,
                    member_type=excluded.member_type,
                    members_json=excluded.members_json,
                    show_url=excluded.show_url,
                    updated_at=excluded.updated_at
                """,
                (
                    show["reference_code"],
                    show.get("schedule_id"),
                    show.get("title") or "Theater Show",
                    show["date"],
                    show.get("start_time"),
                    show.get("end_time"),
                    show.get("member_type"),
                    json.dumps(
                        show.get("members") or [],
                        ensure_ascii=False,
                    ),
                    show.get("show_url"),
                    _utcnow_iso(),
                ),
            )


def _list_upcoming_shows(start_date, end_date):
    with _connect() as conn:
        return conn.execute(
            """
            SELECT * FROM show_schedule
            WHERE show_date BETWEEN ? AND ?
            ORDER BY
                show_date ASC,
                start_time ASC,
                title COLLATE NOCASE ASC
            """,
            (start_date, end_date),
        ).fetchall()


def _set_collector_state(ok, show_count=0, error=None):
    now = _utcnow_iso()

    with _connect() as conn:
        if ok:
            conn.execute(
                """
                UPDATE show_collector_state
                SET
                    last_seen=?,
                    last_success=?,
                    last_error=NULL,
                    show_count=?
                WHERE id=1
                """,
                (now, now, int(show_count)),
            )
        else:
            conn.execute(
                """
                UPDATE show_collector_state
                SET
                    last_seen=?,
                    last_error=?
                WHERE id=1
                """,
                (
                    now,
                    str(error or "Unknown show collector error")[:500],
                ),
            )


def _get_collector_state():
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM show_collector_state
            WHERE id=1
            """
        ).fetchone()

    state = (
        dict(row)
        if row
        else {
            "last_seen": None,
            "last_success": None,
            "last_error": None,
            "show_count": 0,
        }
    )

    status = "waiting"
    last_success = state.get("last_success")

    if last_success:
        try:
            parsed = datetime.fromisoformat(
                str(last_success).replace("Z", "+00:00")
            )
            age = (
                datetime.now(timezone.utc)
                - parsed.astimezone(timezone.utc)
            ).total_seconds()
            status = "live" if age <= 1800 else "stale"
        except Exception:
            status = "stale"

    state["status"] = status
    return state


def show_health():
    try:
        return _get_collector_state()
    except Exception as exc:
        return {
            "status": "error",
            "last_seen": None,
            "last_success": None,
            "last_error": f"{type(exc).__name__}: {exc}",
            "show_count": 0,
        }


def _add_oshi(user_id, member_name):
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO show_oshi_targets(
                user_id,
                member_name,
                created_at
            ) VALUES (?, ?, ?)
            """,
            (
                user_id,
                member_name.strip(),
                _utcnow_iso(),
            ),
        )
        return cur.rowcount == 1


def _list_oshi(user_id):
    with _connect() as conn:
        return conn.execute(
            """
            SELECT *
            FROM show_oshi_targets
            WHERE user_id=?
            ORDER BY member_name COLLATE NOCASE ASC
            """,
            (user_id,),
        ).fetchall()


def _delete_oshi(target_id, user_id):
    with _connect() as conn:
        conn.execute(
            """
            DELETE FROM show_oshi_targets
            WHERE id=? AND user_id=?
            """,
            (target_id, user_id),
        )


def _target_user_ids():
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT user_id
            FROM show_oshi_targets
            ORDER BY user_id
            """
        ).fetchall()

    return [int(row["user_id"]) for row in rows]


def _member_options():
    options = set()

    with _connect() as conn:
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT member_name
                FROM event_state
                WHERE member_name IS NOT NULL
                  AND TRIM(member_name) != ''
                """
            ).fetchall()

            options.update(
                str(row["member_name"]).strip()
                for row in rows
                if str(row["member_name"]).strip()
            )
        except sqlite3.OperationalError:
            pass

        rows = conn.execute(
            "SELECT members_json FROM show_schedule"
        ).fetchall()

    for row in rows:
        try:
            names = json.loads(row["members_json"] or "[]")
        except Exception:
            names = []

        for name in names:
            name = str(name).strip()
            if name:
                options.add(name)

    return sorted(options, key=str.casefold)


def _release_schedule_run(run_key):
    with _connect() as conn:
        conn.execute(
            """
            DELETE FROM scheduled_runs
            WHERE run_key=?
            """,
            (run_key,),
        )


def _current_user(request):
    token = request.cookies.get("session")
    if not token:
        return None

    user_id = read_session_token(token)
    if not user_id:
        return None

    return get_user(user_id)


def _go_login():
    return RedirectResponse("/login", status_code=303)


def _collector_token(request):
    authorization = request.headers.get("Authorization", "")

    if authorization.startswith("Bearer "):
        return authorization[len("Bearer "):].strip()

    return request.headers.get("X-Collector-Token", "").strip()


def _decode_members(show):
    try:
        values = json.loads(show.get("members_json") or "[]")
    except Exception:
        values = []

    return [
        str(value).strip()
        for value in values
        if str(value).strip()
    ]


def _matched_oshis(show, target_names):
    targets = {
        str(name).casefold(): str(name)
        for name in target_names
    }

    return [
        targets[name.casefold()]
        for name in _decode_members(show)
        if name.casefold() in targets
    ]


def _show_start_datetime(show):
    try:
        show_date = str(show.get("show_date") or "")
        start_time = str(show.get("start_time") or "")[:5]

        if not show_date or not start_time:
            return None

        naive = datetime.strptime(
            f"{show_date} {start_time}",
            "%Y-%m-%d %H:%M",
        )

        return naive.replace(
            tzinfo=ZoneInfo(settings.timezone)
        )
    except Exception:
        return None


def _show_block(show, matched):
    members = _decode_members(show)

    lineup = (
        ", ".join(members[:16])
        if members
        else "Belum ada informasi line-up"
    )

    if len(members) > 16:
        lineup += f", +{len(members) - 16} member"

    show_url = show.get("show_url") or ""
    detail_link = (
        f"\n🔗 [Detail Show]({show_url})"
        if show_url
        else ""
    )

    return (
        f"🎭 **{show.get('title') or 'Theater Show'}**\n"
        f"📅 `{show.get('show_date')}`\n"
        f"⏰ `{show.get('start_time') or '-'}"
        f" - {show.get('end_time') or '-'} WIB`\n"
        f"✨ Oshi: **{', '.join(matched)}**\n"
        f"👥 {lineup}"
        f"{detail_link}"
    )


def _discord_post(webhook_url, payload):
    response = requests.post(
        webhook_url,
        params={"wait": "true"},
        json=payload,
        headers={
            "User-Agent": "48Group-Show-Oshi/1.0",
            "Content-Type": "application/json",
        },
        timeout=15,
    )

    if response.status_code not in (200, 204):
        raise RuntimeError(
            f"Discord HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )


def _send_show_embed(
    webhook_url,
    title,
    description,
    content=None,
):
    payload = {
        "embeds": [
            {
                "title": title,
                "description": description,
                "color": COLOR_PINK,
                "footer": {
                    "text": (
                        "48Group Show Oshi "
                        "• Automatic System"
                    )
                },
                "timestamp": datetime.now(
                    timezone.utc
                ).isoformat(),
            }
        ],
        "allowed_mentions": {
            "parse": ["everyone"]
        },
    }

    if content:
        payload["content"] = content

    _discord_post(webhook_url, payload)


def _enabled_hooks(user_id):
    return [
        dict(row)
        for row in list_user_webhooks(user_id)
        if int(row["enabled"])
    ]


def _maybe_send_summaries():
    now = datetime.now(
        ZoneInfo(settings.timezone)
    )

    if now.hour not in (8, 20):
        return

    if now.minute >= 10:
        return

    start_date = now.date()
    end_date = start_date + timedelta(days=14)

    shows = _list_upcoming_shows(
        start_date.isoformat(),
        end_date.isoformat(),
    )

    if not shows:
        return

    for user_id in _target_user_ids():
        targets = [
            row["member_name"]
            for row in _list_oshi(user_id)
        ]

        if not targets:
            continue

        blocks = []

        for row in shows:
            show = dict(row)
            matched = _matched_oshis(show, targets)

            if matched:
                blocks.append(_show_block(show, matched))

        if not blocks:
            continue

        description = "\n\n".join(blocks)[:3900]

        for hook in _enabled_hooks(user_id):
            run_key = (
                f"show-summary:{now.date().isoformat()}:"
                f"{now.hour}:{user_id}:{hook['id']}"
            )

            if not claim_schedule_run(run_key):
                continue

            try:
                _send_show_embed(
                    decrypt_webhook(
                        hook["webhook_url_enc"]
                    ),
                    (
                        "🎭 SHOW OSHI UPDATE "
                        f"• {now.hour:02d}:00 WIB"
                    ),
                    description,
                    content="🎭 **Jadwal show oshi terbaru**",
                )

                log.info(
                    "Show summary terkirim "
                    "user=%s webhook=%s",
                    user_id,
                    hook["id"],
                )
            except Exception:
                _release_schedule_run(run_key)
                log.exception(
                    "Show summary gagal "
                    "user=%s webhook=%s",
                    user_id,
                    hook["id"],
                )


def _maybe_send_reminders():
    now = datetime.now(
        ZoneInfo(settings.timezone)
    )

    shows = _list_upcoming_shows(
        now.date().isoformat(),
        (now.date() + timedelta(days=1)).isoformat(),
    )

    if not shows:
        return

    users = _target_user_ids()
    if not users:
        return

    for row in shows:
        show = dict(row)
        start_at = _show_start_datetime(show)

        if start_at is None:
            continue

        seconds_until = (
            start_at - now
        ).total_seconds()

        if seconds_until <= 0 or seconds_until > 7200:
            continue

        for user_id in users:
            targets = [
                target["member_name"]
                for target in _list_oshi(user_id)
            ]

            matched = _matched_oshis(show, targets)
            if not matched:
                continue

            for hook in _enabled_hooks(user_id):
                run_key = (
                    "show-reminder:"
                    f"{show['reference_code']}:"
                    f"{user_id}:{hook['id']}"
                )

                if not claim_schedule_run(run_key):
                    continue

                try:
                    content = (
                        "@everyone ⏰ **REMINDER SHOW OSHI "
                        "• 2 JAM LAGI!**"
                        if int(hook["mention_everyone"])
                        else
                        "⏰ **REMINDER SHOW OSHI "
                        "• 2 JAM LAGI!**"
                    )

                    _send_show_embed(
                        decrypt_webhook(
                            hook["webhook_url_enc"]
                        ),
                        "⏰ SHOW OSHI REMINDER",
                        (
                            "Show oshi Anda akan dimulai "
                            "sekitar **2 jam lagi**.\n\n"
                            + _show_block(show, matched)
                        ),
                        content=content,
                    )

                    log.info(
                        "Show reminder terkirim "
                        "ref=%s user=%s webhook=%s",
                        show["reference_code"],
                        user_id,
                        hook["id"],
                    )
                except Exception:
                    _release_schedule_run(run_key)
                    log.exception(
                        "Show reminder gagal "
                        "ref=%s user=%s webhook=%s",
                        show["reference_code"],
                        user_id,
                        hook["id"],
                    )


class ShowService:
    def __init__(self):
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return

        self.stop_event.clear()
        self.thread = threading.Thread(
            target=self.run,
            daemon=True,
            name="48group-show-monitor",
        )
        self.thread.start()

    def stop(self):
        self.stop_event.set()

        if self.thread:
            self.thread.join(timeout=10)

    def run(self):
        log.info("Show Oshi scheduler aktif.")

        while not self.stop_event.is_set():
            try:
                _maybe_send_summaries()
                _maybe_send_reminders()
            except Exception:
                log.exception("Show Oshi scheduler error")

            self.stop_event.wait(5)


def start_show_service():
    global _SERVICE

    if _SERVICE is None:
        _SERVICE = ShowService()

    _SERVICE.start()


def stop_show_service():
    if _SERVICE is not None:
        _SERVICE.stop()


@router.post("/api/show-collector/snapshot")
def show_collector_snapshot(
    request: Request,
    payload: dict = Body(...),
):
    token = _collector_token(request)

    if (
        not token
        or not hmac.compare_digest(
            token,
            settings.collector_secret,
        )
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid collector token.",
        )

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400,
            detail="Payload harus JSON object.",
        )

    if not bool(payload.get("ok")):
        error = str(
            payload.get("error")
            or "Show collector gagal mengambil API."
        )

        _set_collector_state(
            ok=False,
            error=error,
        )

        return JSONResponse(
            {
                "ok": True,
                "stored": False,
                "error": error,
            }
        )

    window_start = str(
        payload.get("window_start") or ""
    ).strip()
    window_end = str(
        payload.get("window_end") or ""
    ).strip()
    items = payload.get("items")

    if (
        not window_start
        or not window_end
        or not isinstance(items, list)
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Show payload membutuhkan "
                "window_start, window_end, dan items."
            ),
        )

    try:
        datetime.strptime(window_start, "%Y-%m-%d")
        datetime.strptime(window_end, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Format tanggal window tidak valid.",
        ) from exc

    prepared = []

    for item in items:
        if not isinstance(item, dict):
            continue

        ref = str(
            item.get("reference_code") or ""
        ).strip()
        show_date = str(
            item.get("date") or ""
        ).strip()

        if not ref or not show_date:
            continue

        try:
            datetime.strptime(show_date, "%Y-%m-%d")
        except ValueError:
            continue

        prepared.append(
            {
                **item,
                "reference_code": ref,
                "date": show_date,
            }
        )

    _replace_show_window(
        window_start,
        window_end,
        prepared,
    )

    _set_collector_state(
        ok=True,
        show_count=len(prepared),
    )

    log.info(
        "Show collector diterima: "
        "%s show (%s..%s)",
        len(prepared),
        window_start,
        window_end,
    )

    return JSONResponse(
        {
            "ok": True,
            "stored": True,
            "shows": len(prepared),
        }
    )


@router.get("/shows", response_class=HTMLResponse)
def shows_page(request: Request):
    user = _current_user(request)

    if not user:
        return _go_login()

    now = datetime.now(
        ZoneInfo(settings.timezone)
    )

    rows = _list_upcoming_shows(
        now.date().isoformat(),
        (
            now.date()
            + timedelta(days=14)
        ).isoformat(),
    )

    targets = [
        dict(row)
        for row in _list_oshi(
            user["id"]
        )
    ]

    target_map = {
        row["member_name"].casefold():
        row["member_name"]
        for row in targets
    }

    shows = []

    for row in rows:
        show = dict(row)
        members = _decode_members(show)

        show["members"] = members
        show["matched_oshis"] = [
            target_map[name.casefold()]
            for name in members
            if name.casefold() in target_map
        ]

        shows.append(show)

    hooks = _enabled_hooks(user["id"])

    return templates.TemplateResponse(
        request=request,
        name="shows.html",
        context={
            "user": user,
            "shows": shows,
            "oshi_targets": targets,
            "member_options": _member_options(),
            "active_webhooks": len(hooks),
            "show_state": show_health(),
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/shows/oshi")
def add_oshi_route(
    request: Request,
    member_name: str = Form(...),
):
    user = _current_user(request)

    if not user:
        return _go_login()

    member_name = " ".join(
        member_name.strip().split()
    )

    if len(member_name) < 2 or len(member_name) > 100:
        return RedirectResponse(
            "/shows?error="
            + quote("Nama oshi tidak valid."),
            status_code=303,
        )

    created = _add_oshi(
        user["id"],
        member_name,
    )

    message = (
        f"🎭 {member_name} ditambahkan ke Show Oshi."
        if created
        else f"{member_name} sudah ada di Show Oshi."
    )

    return RedirectResponse(
        "/shows?message="
        + quote(message),
        status_code=303,
    )


@router.post("/shows/oshi/{target_id}/delete")
def delete_oshi_route(
    target_id: int,
    request: Request,
):
    user = _current_user(request)

    if not user:
        return _go_login()

    _delete_oshi(
        target_id,
        user["id"],
    )

    return RedirectResponse(
        "/shows?message="
        + quote("Oshi show dihapus."),
        status_code=303,
    )
