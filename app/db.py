import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from .config import settings


def _connect():
    parent = os.path.dirname(settings.database_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    conn = sqlite3.connect(settings.database_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db():
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS webhooks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                webhook_url_enc TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                notify_jkt INTEGER NOT NULL DEFAULT 1,
                notify_akb INTEGER NOT NULL DEFAULT 1,
                mention_everyone INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS event_state (
                uid TEXT PRIMARY KEY,
                group_name TEXT NOT NULL,
                member_name TEXT NOT NULL,
                session_name TEXT NOT NULL,
                track_name TEXT NOT NULL,
                stock INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS restock_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid TEXT NOT NULL,
                group_name TEXT NOT NULL,
                member_name TEXT NOT NULL,
                session_name TEXT NOT NULL,
                track_name TEXT NOT NULL,
                old_stock INTEGER NOT NULL,
                new_stock INTEGER NOT NULL,
                detected_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scheduled_runs (
                run_key TEXT PRIMARY KEY,
                sent_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS member_targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                group_name TEXT NOT NULL,
                member_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, group_name, member_name)
            );

            CREATE INDEX IF NOT EXISTS idx_member_targets_lookup
            ON member_targets(group_name, member_name);

            CREATE TABLE IF NOT EXISTS mng_weekly_status (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                week_key TEXT NOT NULL,
                member_name TEXT NOT NULL,
                cutoff_at TEXT NOT NULL,
                baseline_session_count INTEGER NOT NULL,
                baseline_session_names TEXT NOT NULL,
                total_stock_at_cutoff INTEGER NOT NULL,
                eligible INTEGER NOT NULL DEFAULT 0,
                maxed INTEGER NOT NULL DEFAULT 0,
                released_at TEXT,
                released_session_names TEXT,
                notified_at TEXT,
                UNIQUE(week_key, member_name)
            );

            CREATE INDEX IF NOT EXISTS idx_mng_weekly_status_week
            ON mng_weekly_status(week_key);
            """
        )

        # Migrasi aman: webhook lama tetap menerima M&G secara default.
        webhook_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(webhooks)").fetchall()
        }
        if "notify_mng" not in webhook_columns:
            conn.execute(
                "ALTER TABLE webhooks "
                "ADD COLUMN notify_mng INTEGER NOT NULL DEFAULT 1"
            )


def utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


def create_user(username, password_hash):
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO users(username,password_hash,created_at) VALUES (?,?,?)",
            (username.strip(), password_hash, utcnow_iso()),
        )
        return int(cur.lastrowid)


def get_user_by_username(username):
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE username=?",
            (username.strip(),),
        ).fetchone()


def get_user(user_id):
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE id=?",
            (user_id,),
        ).fetchone()


def add_webhook(
    user_id,
    name,
    webhook_url_enc,
    notify_jkt,
    notify_akb,
    notify_mng,
    mention_everyone=True,
):
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO webhooks(
                user_id,name,webhook_url_enc,enabled,
                notify_jkt,notify_akb,notify_mng,mention_everyone,created_at
            ) VALUES (?,?,?,1,?,?,?,?,?)
            """,
            (
                user_id,
                name.strip(),
                webhook_url_enc,
                int(notify_jkt),
                int(notify_akb),
                int(notify_mng),
                int(mention_everyone),
                utcnow_iso(),
            ),
        )
        return int(cur.lastrowid)


def list_user_webhooks(user_id):
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM webhooks WHERE user_id=? ORDER BY id DESC",
            (user_id,),
        ).fetchall()


def list_enabled_webhooks(group_name: Optional[str] = None):
    sql = "SELECT * FROM webhooks WHERE enabled=1"

    if group_name == "JKT48":
        sql += " AND notify_jkt=1"
    elif group_name == "AKB48":
        sql += " AND notify_akb=1"
    elif group_name == "JKT48_MNG":
        sql += " AND notify_mng=1"

    with _connect() as conn:
        return conn.execute(sql).fetchall()


def get_webhook_for_user(webhook_id, user_id):
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM webhooks WHERE id=? AND user_id=?",
            (webhook_id, user_id),
        ).fetchone()


def toggle_webhook(webhook_id, user_id):
    with _connect() as conn:
        conn.execute(
            """
            UPDATE webhooks
            SET enabled=CASE enabled WHEN 1 THEN 0 ELSE 1 END
            WHERE id=? AND user_id=?
            """,
            (webhook_id, user_id),
        )


def delete_webhook(webhook_id, user_id):
    with _connect() as conn:
        conn.execute(
            "DELETE FROM webhooks WHERE id=? AND user_id=?",
            (webhook_id, user_id),
        )


def load_event_state():
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM event_state").fetchall()
        return {row["uid"]: dict(row) for row in rows}


def upsert_event_state(item):
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO event_state(
                uid,group_name,member_name,session_name,track_name,stock,updated_at
            ) VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(uid) DO UPDATE SET
                group_name=excluded.group_name,
                member_name=excluded.member_name,
                session_name=excluded.session_name,
                track_name=excluded.track_name,
                stock=excluded.stock,
                updated_at=excluded.updated_at
            """,
            (
                item["id"],
                item["group"],
                item["name"],
                item["session"],
                item["track"],
                item["stock"],
                utcnow_iso(),
            ),
        )


def add_restock_log(item, old_stock, new_stock):
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO restock_log(
                uid,group_name,member_name,session_name,track_name,
                old_stock,new_stock,detected_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                item["id"],
                item["group"],
                item["name"],
                item["session"],
                item["track"],
                old_stock,
                new_stock,
                utcnow_iso(),
            ),
        )


def recent_restocks(limit=20):
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM restock_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()


def claim_schedule_run(run_key: str) -> bool:
    """Atomically claim one scheduled report slot; survives app restarts."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO scheduled_runs(run_key, sent_at) VALUES (?, ?)",
            (run_key, utcnow_iso()),
        )
        return cur.rowcount == 1


def add_member_target(user_id: int, group_name: str, member_name: str) -> bool:
    """Add one sniping target. Returns False when it already exists."""
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO member_targets(
                user_id, group_name, member_name, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (user_id, group_name.strip(), member_name.strip(), utcnow_iso()),
        )
        return cur.rowcount == 1


def list_member_targets(user_id: int):
    with _connect() as conn:
        return conn.execute(
            """
            SELECT * FROM member_targets
            WHERE user_id=?
            ORDER BY group_name ASC, member_name COLLATE NOCASE ASC
            """,
            (user_id,),
        ).fetchall()


def delete_member_target(target_id: int, user_id: int):
    with _connect() as conn:
        conn.execute(
            "DELETE FROM member_targets WHERE id=? AND user_id=?",
            (target_id, user_id),
        )


def target_user_ids(group_name: str, member_name: str):
    """Return users that are actively sniping this exact member."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT user_id FROM member_targets
            WHERE group_name=? AND member_name=?
            """,
            (group_name, member_name),
        ).fetchall()
        return {int(row["user_id"]) for row in rows}



def mng_week_has_cutoff(week_key: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM mng_weekly_status WHERE week_key=? LIMIT 1",
            (week_key,),
        ).fetchone()
        return row is not None


def save_mng_weekly_cutoff(week_key: str, cutoff_at: str, rows: list[dict]):
    with _connect() as conn:
        inserted = 0
        for row in rows:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO mng_weekly_status(
                    week_key, member_name, cutoff_at,
                    baseline_session_count, baseline_session_names,
                    total_stock_at_cutoff, eligible, maxed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    week_key,
                    row["member_name"],
                    cutoff_at,
                    int(row["session_count"]),
                    row["session_names_json"],
                    int(row["total_stock"]),
                    int(row["eligible"]),
                    int(row["maxed"]),
                ),
            )
            inserted += cur.rowcount
        return inserted


def list_mng_weekly_status(week_key: str):
    with _connect() as conn:
        return conn.execute(
            """
            SELECT * FROM mng_weekly_status
            WHERE week_key=?
            ORDER BY eligible DESC, maxed DESC, member_name COLLATE NOCASE ASC
            """,
            (week_key,),
        ).fetchall()


def mark_mng_extra_session_live(
    week_key: str,
    member_name: str,
    released_at: str,
    released_session_names: str,
) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE mng_weekly_status
            SET released_at=?, released_session_names=?
            WHERE week_key=? AND member_name=?
              AND eligible=1 AND released_at IS NULL
            """,
            (released_at, released_session_names, week_key, member_name),
        )
        return cur.rowcount == 1


def mark_mng_release_notified(week_key: str, member_name: str):
    with _connect() as conn:
        conn.execute(
            """
            UPDATE mng_weekly_status
            SET notified_at=?
            WHERE week_key=? AND member_name=?
            """,
            (utcnow_iso(), week_key, member_name),
        )
