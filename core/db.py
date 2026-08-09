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

            CREATE TABLE IF NOT EXISTS leads (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id    TEXT NOT NULL,
                source          TEXT NOT NULL,             -- adzuna | remoteok | company_manual
                external_id     TEXT,                      -- source's own id -- dedup key alongside source
                title           TEXT NOT NULL,
                company         TEXT NOT NULL,
                location        TEXT,
                description     TEXT,
                url             TEXT,
                posted_at       TEXT,
                score           REAL NOT NULL,
                matched_skills  TEXT,                      -- JSON array
                reasons         TEXT,                      -- JSON array, human-readable "why this scored well"
                status          TEXT DEFAULT 'new',         -- new | approved | rejected | expired | contacted
                found_at        TEXT DEFAULT CURRENT_TIMESTAMP,
                decided_at      TEXT,                       -- when user approved/rejected -- drives the 48h expiry check
                UNIQUE(source, external_id, candidate_id)    -- re-running a search won't duplicate the same posting
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


def save_lead(candidate_id: str, scored_lead) -> int | None:
    """Insert a scored lead. Returns None (not an error) if it's a dupe of
    a lead already found for this candidate -- re-running lead_finder
    shouldn't flood the approval queue with postings already seen."""
    lead = scored_lead.lead
    with get_conn() as conn:
        try:
            cur = conn.execute(
                """INSERT INTO leads
                   (candidate_id, source, external_id, title, company, location,
                    description, url, posted_at, score, matched_skills, reasons, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    candidate_id, lead.source, lead.external_id, lead.title, lead.company,
                    lead.location, lead.description, lead.url, lead.posted_at,
                    scored_lead.score, json.dumps(scored_lead.matched_skills),
                    json.dumps(scored_lead.reasons), scored_lead.status,
                ),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            # UNIQUE(source, external_id, candidate_id) tripped -- already have this one
            return None


def get_leads(candidate_id: str, status: str = None) -> list[dict]:
    with get_conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM leads WHERE candidate_id = ? AND status = ? ORDER BY score DESC",
                (candidate_id, status),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM leads WHERE candidate_id = ? ORDER BY score DESC",
                (candidate_id,),
            ).fetchall()
        return [dict(r) for r in rows]


def update_lead_status(lead_id: int, status: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE leads SET status = ?, decided_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, lead_id),
        )
