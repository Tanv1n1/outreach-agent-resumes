"""
Finds leads that were sent but never replied to, and after enough silence
(settings.FOLLOWUP_AFTER_DAYS), drafts a short follow-up message for the
SAME human-approval flow as everything else -- this module never sends
anything on its own, it only prepares.

Deliberately reuses generate_verified_message's rule+judge checks, just
with a follow-up-specific system-prompt framing (references the earlier
message, doesn't repeat the full pitch, stays short) rather than a
separate implementation.
"""

import logging
from datetime import datetime, timedelta

from core import db
from core.models import CandidateProfile
from config import settings

logger = logging.getLogger(__name__)


def find_leads_needing_followup(candidate_id: str) -> list[dict]:
    """Sent, unreplied, past the silence threshold, and not already
    followed up on. Returns the raw draft rows -- caller decides whether
    to actually generate+send a follow-up for each."""
    awaiting = db.get_sent_awaiting_reply(candidate_id)
    cutoff = datetime.utcnow() - timedelta(days=settings.FOLLOWUP_AFTER_DAYS)

    due = []
    for draft in awaiting:
        if draft.get("follow_up_sent_at"):
            continue   # already followed up once -- don't nag repeatedly
        created_at = draft.get("created_at")
        if not created_at:
            continue
        try:
            sent_dt = datetime.fromisoformat(created_at)
        except ValueError:
            continue
        if sent_dt <= cutoff:
            due.append(draft)

    logger.info("%d lead(s) due for a follow-up (silent %d+ days)", len(due), settings.FOLLOWUP_AFTER_DAYS)
    return due


def draft_followup(profile: CandidateProfile, lead: dict, original_draft: dict) -> dict:
    """Generates a follow-up draft through the same verified-loop drafter,
    with a follow-up-specific instruction appended so it doesn't just
    re-pitch the same message from scratch."""
    from services.message_gen.drafter import generate_verified_message

    followup_lead = dict(lead)
    # Injected into the job-description context the drafter already reads,
    # rather than plumbing a whole new prompt path through -- the drafter's
    # existing context-builder picks this up naturally.
    followup_lead["description"] = (
        (lead.get("description") or "") +
        f"\n\n[SYSTEM NOTE: This is a FOLLOW-UP to a message already sent "
        f"{settings.FOLLOWUP_AFTER_DAYS}+ days ago with no reply. Keep it "
        f"short (3-4 sentences), reference that you reached out before "
        f"without repeating the full original pitch, and gently re-ask. "
        f"Original message subject was: {original_draft.get('subject', '')}]"
    )
    return generate_verified_message(profile, followup_lead)