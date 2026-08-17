"""
Decides what "sending" actually means for a given lead, and does it.
Three real outcomes, no pretending a 4th exists:

1. AUTO-SENT TO HR -- only when the job posting's own text explicitly
   states a contact email (e.g. "send your resume to jobs@company.com").
   Regex-extracted, never guessed -- see module docstring in verifier.py
   for the same "don't fabricate what wasn't given" principle applied
   here to contact info instead of resume facts.

2. EMAILED TO YOU (digest) -- the honest fallback for the common case:
   no stated email, and (as established) there's no legitimate API for
   auto-submitting through Greenhouse/Lever/etc.'s own application forms
   (their submit-application endpoints require an employer-issued API
   key that job seekers can never obtain). Rather than attempt a fragile
   form-fill bot, the candidate gets a clean email with the apply link
   so it's easy to action manually and doesn't get lost in Telegram scroll.

3. MANUAL ONLY -- leads sourced via manual_lead.py (LinkedIn/Naukri finds
   the user pasted in themselves). These were explicitly carved out by
   the user as "I'll handle these myself" -- dispatch does nothing for
   these except confirm that's the plan.
"""

import re
import logging
from pathlib import Path

from core import db
from core.models import CandidateProfile
from config import settings
from services.dispatch.email_sender import send_email, EmailConfigError, EmailSendError
from services.storage import backblaze_client as b2

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

# Generic platform addresses that show up in job description boilerplate
# (e.g. Greenhouse's own noreply footer) but are never a real HR contact --
# treated as "no email found", not a valid send target.
_IGNORED_EMAIL_DOMAINS = {"greenhouse.io", "lever.co", "noreply", "example.com"}


def extract_stated_email(description: str) -> str | None:
    """Returns the first real-looking email explicitly present in the job
    description text, or None. Never fabricates or guesses one."""
    if not description:
        return None
    for match in _EMAIL_RE.findall(description):
        domain = match.split("@")[-1].lower()
        if any(ignored in domain for ignored in _IGNORED_EMAIL_DOMAINS):
            continue
        return match
    return None


def dispatch_lead(lead: dict, draft: dict, profile: CandidateProfile) -> dict:
    """Executes the appropriate path for this lead and returns a result
    dict the caller (telegram_bot.py) uses to tell the user what actually
    happened -- never a vague "sent!" when what really happened was
    "emailed you a link because there was nowhere else to send it."""

    if lead["source"] == "manual":
        db.update_draft_status(lead["id"], "manual_only")
        return {"method": "manual_only", "detail": "You added this lead yourself -- apply/send it directly."}

    stated_email = extract_stated_email(lead.get("description", ""))

    if stated_email:
        resume_path = _resolve_resume_path(lead["candidate_id"])
        try:
            message_id = send_email(
                stated_email, draft.get("subject") or f"Re: {lead['title']}",
                draft["body"], attachment_path=resume_path,
            )
        except (EmailConfigError, EmailSendError) as e:
            logger.warning("Direct HR send failed for lead %s: %s", lead["id"], e)
            return _fallback_to_digest(lead, profile, reason=str(e))

        db.update_draft_status(lead["id"], "sent")
        db.set_sent_message_id(lead["id"], message_id)
        return {"method": "auto_sent_to_hr", "sent_to": stated_email, "resume_attached": bool(resume_path)}

    return _fallback_to_digest(lead, profile)


def _resolve_resume_path(candidate_id: str) -> str | None:
    """Finds an actual resume file to attach: tries the local path first
    (fast, no network), falls back to downloading from B2 if the local
    file is gone (e.g. running on a different machine, or the scratch
    file was cleaned up). Returns None if neither works -- the caller
    treats that as "send without an attachment", not a hard failure."""
    upload = db.get_latest_resume_upload(candidate_id)
    if not upload:
        logger.warning("No resume upload record found for candidate %s -- sending without attachment", candidate_id)
        return None

    local_path = upload.get("file_path")
    if local_path and Path(local_path).exists():
        return local_path

    b2_key = upload.get("b2_key")
    if not b2_key:
        logger.warning("Resume for candidate %s missing locally and no B2 backup exists -- sending without attachment", candidate_id)
        return None

    dest_dir = Path(settings.LOCAL_UPLOAD_DIR) / "_dispatch_cache"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / Path(b2_key).name
    try:
        b2.download_resume(b2_key, str(dest_path))
        return str(dest_path)
    except Exception as e:
        logger.warning("Could not fetch resume from B2 for candidate %s (%s) -- sending without attachment", candidate_id, e)
        return None


def _fallback_to_digest(lead: dict, profile: CandidateProfile, reason: str = None) -> dict:
    recipient = settings.NOTIFY_EMAIL or profile.email
    subject = f"Apply: {lead['title']} @ {lead['company']}"
    body = (
        f"No direct HR contact was found for this one, so here's the link to apply yourself:\n\n"
        f"{lead['title']} @ {lead['company']}\n"
        f"Location: {lead.get('location') or 'n/a'}\n"
        f"Score: {lead.get('score')}\n\n"
        f"Apply here: {lead.get('url') or '(no link available)'}"
    )
    try:
        send_email(recipient, subject, body)
    except (EmailConfigError, EmailSendError) as e:
        logger.warning("Digest email failed for lead %s: %s", lead["id"], e)
        db.update_draft_status(lead["id"], "digest_failed")
        return {"method": "digest_failed", "detail": str(e)}

    db.update_draft_status(lead["id"], "digested_to_user")
    result = {"method": "emailed_to_user", "sent_to": recipient, "apply_link": lead.get("url")}
    if reason:
        result["fallback_reason"] = reason
    return result