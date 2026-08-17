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

            CREATE TABLE IF NOT EXISTS telegram_config (
                key    TEXT PRIMARY KEY,
                value  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS message_drafts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id       INTEGER NOT NULL,
                candidate_id  TEXT NOT NULL,
                subject       TEXT,                          -- null for non-email channels
                body          TEXT NOT NULL,
                channel       TEXT DEFAULT 'email',           -- email | linkedin
                status        TEXT DEFAULT 'pending_review',  -- pending_review | approved_to_send | sent | digested_to_user | digest_failed | manual_only | discarded
                sent_message_id  TEXT,                        -- RFC Message-ID of the sent email -- used to match incoming replies via In-Reply-To/References
                replied          INTEGER DEFAULT 0,
                reply_classification TEXT,                    -- interested | not_interested | auto_reply | other
                reply_snippet    TEXT,
                follow_up_sent_at TEXT,
                created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(lead_id)                                -- one active draft per lead -- regenerate replaces it, doesn't stack
            );
        """)
    _migrate_message_drafts_columns()


def _migrate_message_drafts_columns():
    """Adds columns to message_drafts that were introduced after some users
    already had the table created by an earlier version of this schema.
    CREATE TABLE IF NOT EXISTS alone won't retrofit an existing table, so
    this runs ALTER TABLE ADD COLUMN for each new column, silently skipping
    ones that already exist (SQLite has no ADD COLUMN IF NOT EXISTS)."""
    new_columns = [
        ("sent_message_id", "TEXT"),
        ("replied", "INTEGER DEFAULT 0"),
        ("reply_classification", "TEXT"),
        ("reply_snippet", "TEXT"),
        ("follow_up_sent_at", "TEXT"),
    ]
    with get_conn() as conn:
        for col_name, col_type in new_columns:
            try:
                conn.execute(f"ALTER TABLE message_drafts ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e):
                    raise   # anything other than "already exists" is a real problem


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


def get_lead(lead_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        return dict(row) if row else None


def set_telegram_chat_id(chat_id: int):
    """Stored once, the first time the user sends /start to the bot --
    there's no way to know their chat_id in advance. Single-user system,
    so a single stored value (not per-candidate) is the right shape."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO telegram_config (key, value) VALUES ('chat_id', ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (str(chat_id),),
        )


def get_telegram_chat_id() -> int | None:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM telegram_config WHERE key = 'chat_id'").fetchone()
        return int(row["value"]) if row else None


def save_draft(lead_id: int, candidate_id: str, subject: str, body: str, channel: str = "email") -> int:
    """UPSERT on lead_id -- regenerating a draft replaces the previous one
    rather than stacking duplicates, since only the latest draft is ever
    relevant (there's no history/versioning need here)."""
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO message_drafts (lead_id, candidate_id, subject, body, channel)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(lead_id) DO UPDATE SET
                 subject = excluded.subject, body = excluded.body,
                 channel = excluded.channel, status = 'pending_review'""",
            (lead_id, candidate_id, subject, body, channel),
        )
        return cur.lastrowid


def get_draft_by_lead(lead_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM message_drafts WHERE lead_id = ?", (lead_id,)).fetchone()
        return dict(row) if row else None


def update_draft_status(lead_id: int, status: str):
    with get_conn() as conn:
        conn.execute("UPDATE message_drafts SET status = ? WHERE lead_id = ?", (status, lead_id))


def set_sent_message_id(lead_id: int, message_id: str):
    with get_conn() as conn:
        conn.execute("UPDATE message_drafts SET sent_message_id = ? WHERE lead_id = ?", (message_id, lead_id))


def get_draft_by_message_id(message_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM message_drafts WHERE sent_message_id = ?", (message_id,)
        ).fetchone()
        return dict(row) if row else None


def mark_reply_received(lead_id: int, classification: str, snippet: str):
    with get_conn() as conn:
        conn.execute(
            """UPDATE message_drafts SET replied = 1, reply_classification = ?,
               reply_snippet = ? WHERE lead_id = ?""",
            (classification, snippet, lead_id),
        )


def get_sent_awaiting_reply(candidate_id: str) -> list[dict]:
    """Drafts that were actually sent (auto_sent_to_hr path only -- digest
    emails to the user don't get a real HR reply to wait for) and haven't
    been replied to yet. Feeds both the reply-poll matching step and the
    follow-up-after-silence step."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM message_drafts
               WHERE candidate_id = ? AND status = 'sent' AND replied = 0
               AND sent_message_id IS NOT NULL""",
            (candidate_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_followup_sent(lead_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE message_drafts SET follow_up_sent_at = CURRENT_TIMESTAMP WHERE lead_id = ?",
            (lead_id,),
        )