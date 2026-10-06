import os, sqlite3
from datetime import datetime, timezone
from typing import Optional
from .config import settings

def _connect():
    parent = os.path.dirname(settings.database_path)
    if parent: os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(settings.database_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn

def init_db():
    with _connect() as conn:
        conn.executescript("""
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
        """)

def utcnow_iso(): return datetime.now(timezone.utc).isoformat()

def create_user(username, password_hash):
    with _connect() as conn:
        cur = conn.execute("INSERT INTO users(username,password_hash,created_at) VALUES (?,?,?)", (username.strip(), password_hash, utcnow_iso()))
        return int(cur.lastrowid)

def get_user_by_username(username):
    with _connect() as conn: return conn.execute("SELECT * FROM users WHERE username=?", (username.strip(),)).fetchone()

def get_user(user_id):
    with _connect() as conn: return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()

def add_webhook(user_id, name, webhook_url_enc, notify_jkt, notify_akb, mention_everyone=True):
    with _connect() as conn:
        cur = conn.execute("""INSERT INTO webhooks(user_id,name,webhook_url_enc,enabled,notify_jkt,notify_akb,mention_everyone,created_at)
                            VALUES (?,?,?,1,?,?,?,?)""", (user_id, name.strip(), webhook_url_enc, int(notify_jkt), int(notify_akb), int(mention_everyone), utcnow_iso()))
        return int(cur.lastrowid)

def list_user_webhooks(user_id):
    with _connect() as conn: return conn.execute("SELECT * FROM webhooks WHERE user_id=? ORDER BY id DESC", (user_id,)).fetchall()

def list_enabled_webhooks(group_name: Optional[str]=None):
    sql = "SELECT * FROM webhooks WHERE enabled=1"
    if group_name == "JKT48": sql += " AND notify_jkt=1"
    elif group_name == "AKB48": sql += " AND notify_akb=1"
    with _connect() as conn: return conn.execute(sql).fetchall()

def get_webhook_for_user(webhook_id, user_id):
    with _connect() as conn: return conn.execute("SELECT * FROM webhooks WHERE id=? AND user_id=?", (webhook_id,user_id)).fetchone()

def toggle_webhook(webhook_id, user_id):
    with _connect() as conn: conn.execute("UPDATE webhooks SET enabled=CASE enabled WHEN 1 THEN 0 ELSE 1 END WHERE id=? AND user_id=?", (webhook_id,user_id))

def delete_webhook(webhook_id, user_id):
    with _connect() as conn: conn.execute("DELETE FROM webhooks WHERE id=? AND user_id=?", (webhook_id,user_id))

def load_event_state():
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM event_state").fetchall()
        return {r["uid"]: dict(r) for r in rows}

def upsert_event_state(item):
    with _connect() as conn:
        conn.execute("""INSERT INTO event_state(uid,group_name,member_name,session_name,track_name,stock,updated_at)
                        VALUES (?,?,?,?,?,?,?)
                        ON CONFLICT(uid) DO UPDATE SET group_name=excluded.group_name,member_name=excluded.member_name,
                        session_name=excluded.session_name,track_name=excluded.track_name,stock=excluded.stock,updated_at=excluded.updated_at""",
                     (item["id"],item["group"],item["name"],item["session"],item["track"],item["stock"],utcnow_iso()))

def add_restock_log(item, old_stock, new_stock):
    with _connect() as conn:
        conn.execute("""INSERT INTO restock_log(uid,group_name,member_name,session_name,track_name,old_stock,new_stock,detected_at)
                        VALUES (?,?,?,?,?,?,?,?)""",
                     (item["id"],item["group"],item["name"],item["session"],item["track"],old_stock,new_stock,utcnow_iso()))

def recent_restocks(limit=20):
    with _connect() as conn: return conn.execute("SELECT * FROM restock_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def claim_schedule_run(run_key: str) -> bool:
    """Atomically claim one scheduled report slot; survives app restarts."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO scheduled_runs(run_key, sent_at) VALUES (?, ?)",
            (run_key, utcnow_iso()),
        )
        return cur.rowcount == 1
