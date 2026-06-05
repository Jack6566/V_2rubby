"""SQLite storage: the user's own Rubika account(s) + panel settings.

Deliberately minimal: just `accounts` and a single-row `settings`.
No proxy tables, no broadcast queues — this is a small personal tool.
"""
import os
import sqlite3
from datetime import datetime

import config

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "data.db")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init():
    conn = _conn()
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            phone     TEXT UNIQUE,
            name      TEXT,
            user_id   TEXT,
            session   TEXT,
            added_at  TEXT,
            status    TEXT DEFAULT 'active'
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            id         INTEGER PRIMARY KEY CHECK (id = 1),
            send_delay REAL,
            marker     TEXT
        )
        """
    )
    c.execute(
        "INSERT OR IGNORE INTO settings (id, send_delay, marker) VALUES (1, ?, ?)",
        (config.DEFAULT_DELAY, config.FORWARD_MARKER),
    )
    conn.commit()
    conn.close()


# ---------- accounts ----------

def add_account(phone: str, name: str, user_id: str, session: str) -> int:
    conn = _conn()
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO accounts (phone, name, user_id, session, added_at, status)
        VALUES (?, ?, ?, ?, ?, 'active')
        ON CONFLICT(phone) DO UPDATE SET
            name=excluded.name,
            user_id=excluded.user_id,
            session=excluded.session,
            status='active'
        """,
        (phone, name, user_id, session, _now()),
    )
    conn.commit()
    row = c.execute("SELECT id FROM accounts WHERE phone = ?", (phone,)).fetchone()
    conn.close()
    return row["id"]


def list_accounts() -> list:
    conn = _conn()
    rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_account(account_id: int):
    conn = _conn()
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_account(account_id: int):
    conn = _conn()
    conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    conn.commit()
    conn.close()


def set_status(account_id: int, status: str):
    conn = _conn()
    conn.execute("UPDATE accounts SET status = ? WHERE id = ?", (status, account_id))
    conn.commit()
    conn.close()


# ---------- settings ----------

def get_settings() -> dict:
    conn = _conn()
    row = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
    conn.close()
    if not row:
        return {"send_delay": config.DEFAULT_DELAY, "marker": config.FORWARD_MARKER}
    return dict(row)


def get_delay() -> float:
    return config.clamp_delay(get_settings().get("send_delay"))


def set_delay(value: float):
    conn = _conn()
    conn.execute("UPDATE settings SET send_delay = ? WHERE id = 1",
                 (config.clamp_delay(value),))
    conn.commit()
    conn.close()


def get_marker() -> str:
    return (get_settings().get("marker") or config.FORWARD_MARKER).strip()


def set_marker(marker: str):
    conn = _conn()
    conn.execute("UPDATE settings SET marker = ? WHERE id = 1", (marker.strip(),))
    conn.commit()
    conn.close()
