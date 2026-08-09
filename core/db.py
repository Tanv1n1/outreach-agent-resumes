"""
Thin persistence layer. SQLite on purpose -- this is a single-user agent
(you), not a multi-tenant product. No need for Postgres/ORM overhead yet.

If this ever needs to run for multiple users, swap DB_PATH for a real
connection string and the table schema mostly carries over as-is.

Every other service imports get_conn() from here rather than opening
its own sqlite3.connect() -- keeps the WAL/lock behavior consistent.
"""

import sqlite3
import json
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(__file__).parent.parent / "data" / "agent.db"


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")   # avoid locked-db errors when
                                                # reply_watcher polls while
                                                # telegram_bot writes
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS candidate_profile (
                candidate_id TEXT PRIMARY KEY,
                data_json    TEXT NOT NULL,
                created_at   TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS resume_uploads (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id  TEXT NOT NULL,
                file_path     TEXT NOT NULL,
                b2_key        TEXT,                      -- set once B2 upload succeeds; null if B2 skipped/failed
                status        TEXT DEFAULT 'pending',  -- pending | parsed | failed
                error         TEXT,
                uploaded_at   TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)


def save_profile(candidate_id: str, profile_json: str):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO candidate_profile (candidate_id, data_json)
               VALUES (?, ?)
               ON CONFLICT(candidate_id) DO UPDATE SET data_json = excluded.data_json""",
            (candidate_id, profile_json),
        )


def get_profile(candidate_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT data_json FROM candidate_profile WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        return json.loads(row["data_json"]) if row else None


def log_upload(candidate_id: str, file_path: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO resume_uploads (candidate_id, file_path) VALUES (?, ?)",
            (candidate_id, file_path),
        )
        return cur.lastrowid


def mark_upload_status(upload_id: int, status: str, error: str = None):
    with get_conn() as conn:
        conn.execute(
            "UPDATE resume_uploads SET status = ?, error = ? WHERE id = ?",
            (status, error, upload_id),
        )


def set_b2_key(upload_id: int, b2_key: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE resume_uploads SET b2_key = ? WHERE id = ?",
            (b2_key, upload_id),
        )
