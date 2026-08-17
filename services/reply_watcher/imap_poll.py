"""
Polls the candidate's inbox (IMAP) for replies to messages this agent
sent, matches them back to a specific lead, and classifies the reply
with Groq (interested / not_interested / auto_reply / other).

Matching mechanism: when dispatch.py sends an email, it generates and
stores a Message-ID (see email_sender.py). A genuine reply's own
In-Reply-To or References header echoes that ID back -- this is how
every email client threads conversations, so it's a reliable match
without needing to parse subject lines or guess.

This module only reads the inbox (IMAP SEARCH + FETCH) -- it never
sends anything itself. Classification results get written back via
core/db.py's mark_reply_received, and the caller (run_reply_watcher.py
or a Telegram command) decides how to notify the user.
"""

import re
import email
import imaplib
import logging
from email.header import decode_header

from groq import Groq
from pydantic import BaseModel

from core import db
from config import settings

logger = logging.getLogger(__name__)


class ReplyClassification(BaseModel):
    classification: str   # interested | not_interested | auto_reply | other
    summary: str           # one-line human-readable summary of what the reply says


_MSGID_RE = re.compile(r"<[^>]+>")


def _decode_str(value) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    decoded = ""
    for text, charset in parts:
        if isinstance(text, bytes):
            decoded += text.decode(charset or "utf-8", errors="ignore")
        else:
            decoded += text
    return decoded


def check_for_replies(candidate_id: str) -> list[dict]:
    """Connects to IMAP, looks at recent inbox messages, matches any that
    are replies to something we sent (via In-Reply-To/References), and
    classifies genuine matches. Returns a list of {lead_id, classification,
    summary, from_} dicts -- one per newly detected reply.

    Only checks leads that are actually awaiting a reply (db.get_sent_awaiting_reply)
    -- doesn't scan the whole inbox against everything, just what's relevant.
    """
    awaiting = db.get_sent_awaiting_reply(candidate_id)
    if not awaiting:
        logger.info("No sent-and-awaiting-reply leads for candidate %s -- skipping IMAP check", candidate_id)
        return []

    # index by message-id for O(1) lookup per inbox message
    awaiting_by_msgid = {d["sent_message_id"]: d for d in awaiting if d.get("sent_message_id")}

    if not (settings.SMTP_USER and settings.SMTP_PASSWORD):
        logger.warning("IMAP check skipped -- SMTP_USER/SMTP_PASSWORD not set (same creds used for IMAP)")
        return []

    found = []
    try:
        conn = imaplib.IMAP4_SSL(settings.IMAP_HOST, settings.IMAP_PORT)
        conn.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        conn.select("INBOX")

        # Recent unseen mail is the common case -- reduces load on repeated
        # polls compared to re-scanning the whole inbox every time.
        status, data = conn.search(None, "UNSEEN")
        if status != "OK":
            logger.warning("IMAP search failed: %s", status)
            return []

        message_nums = data[0].split()
        for num in message_nums:
            status, msg_data = conn.fetch(num, "(RFC822)")
            if status != "OK":
                continue
            msg = email.message_from_bytes(msg_data[0][1])

            in_reply_to = msg.get("In-Reply-To", "")
            references = msg.get("References", "")
            candidate_ids_in_headers = _MSGID_RE.findall(in_reply_to + " " + references)

            matched_draft = None
            for mid in candidate_ids_in_headers:
                if mid in awaiting_by_msgid:
                    matched_draft = awaiting_by_msgid[mid]
                    break

            if not matched_draft:
                continue   # not a reply to anything we sent -- ignore

            body_text = _extract_plain_text(msg)
            from_ = _decode_str(msg.get("From", ""))

            try:
                verdict = _classify_reply(body_text)
            except Exception as e:
                logger.warning("Reply classification failed for lead %s: %s -- storing as 'other'", matched_draft["lead_id"], e)
                verdict = ReplyClassification(classification="other", summary="(classification failed -- read manually)")

            db.mark_reply_received(matched_draft["lead_id"], verdict.classification, verdict.summary)
            found.append({
                "lead_id": matched_draft["lead_id"],
                "classification": verdict.classification,
                "summary": verdict.summary,
                "from_": from_,
            })
            logger.info("Reply matched to lead %s: %s -- %s", matched_draft["lead_id"], verdict.classification, verdict.summary)

        conn.logout()
    except imaplib.IMAP4.error as e:
        logger.warning("IMAP connection/login failed: %s", e)
        return found

    return found


def _extract_plain_text(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get("Content-Disposition"):
                try:
                    return part.get_payload(decode=True).decode(errors="ignore")
                except Exception:
                    continue
        return ""
    try:
        return msg.get_payload(decode=True).decode(errors="ignore")
    except Exception:
        return ""


def _classify_reply(body_text: str) -> ReplyClassification:
    client = Groq(api_key=settings.GROQ_API_KEY, timeout=30.0)
    system_prompt = """Classify this email reply to a job outreach message.

classification must be exactly one of:
- "interested" -- wants to talk further, schedule a call, asks for more info positively
- "not_interested" -- explicit rejection, "not a fit", "position filled", etc.
- "auto_reply" -- out-of-office, automated acknowledgment, no real human response yet
- "other" -- anything that doesn't clearly fit the above (ambiguous, off-topic, etc.)

summary: one short sentence describing what the reply actually says.

Return ONLY valid JSON: {"classification": string, "summary": string}
"""
    # truncate -- classification doesn't need a full email thread/signature block
    response = client.chat.completions.create(
        model=settings.GROQ_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": body_text[:2000]},
        ],
    )
    return ReplyClassification.model_validate_json(response.choices[0].message.content)