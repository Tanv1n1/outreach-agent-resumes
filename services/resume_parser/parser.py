"""
Public entry point for the resume_parser service.
Everything else in the codebase should import `parse_resume` from HERE,
not reach into file_loader/extractor directly -- keeps the internal
split (loading vs extraction) free to change without breaking callers.
"""

import uuid
import logging

from core import db
from core.models import CandidateProfile
from services.resume_parser.file_loader import (
    load_text, UnsupportedFormatError, EmptyResumeError,
)
from services.resume_parser.extractor import extract_profile, ExtractionError
from services.storage import backblaze_client as b2

logger = logging.getLogger(__name__)


class ResumeParseResult:
    """Wraps success/failure so callers (Telegram bot, CLI) can branch
    on `.ok` instead of catching exceptions across module boundaries."""

    def __init__(self, ok: bool, profile: CandidateProfile = None, error: str = None):
        self.ok = ok
        self.profile = profile
        self.error = error


def parse_resume(file_path: str, candidate_id: str = None) -> ResumeParseResult:
    candidate_id = candidate_id or str(uuid.uuid4())
    upload_id = db.log_upload(candidate_id, file_path)

    try:
        raw_text = load_text(file_path)
        profile = extract_profile(raw_text, candidate_id, file_path)

        db.save_profile(candidate_id, profile.to_json())
        db.mark_upload_status(upload_id, "parsed")
        logger.info("Profile saved for candidate %s", candidate_id)

        logger.info("Backing up resume file to B2...")
        _try_backup_to_b2(upload_id, file_path, candidate_id)

        if profile.parse_warnings:
            logger.warning(
                "Profile %s parsed with warnings: %s",
                candidate_id, profile.parse_warnings,
            )
        return ResumeParseResult(ok=True, profile=profile)

    except (UnsupportedFormatError, EmptyResumeError) as e:
        # user-fixable errors -- bad file, not a system failure
        db.mark_upload_status(upload_id, "failed", str(e))
        return ResumeParseResult(ok=False, error=str(e))

    except ExtractionError as e:
        # LLM output was malformed -- retry once before giving up,
        # transient issues (truncation, etc.) are common enough to
        # not immediately bounce back to the user
        logger.warning("Extraction failed, retrying once: %s", e)
        try:
            raw_text = load_text(file_path)
            profile = extract_profile(raw_text, candidate_id, file_path)
            db.save_profile(candidate_id, profile.to_json())
            db.mark_upload_status(upload_id, "parsed")
            return ResumeParseResult(ok=True, profile=profile)
        except Exception as retry_err:
            db.mark_upload_status(upload_id, "failed", str(retry_err))
            return ResumeParseResult(
                ok=False,
                error="Couldn't parse this resume after retrying. "
                      "Try a cleaner PDF/DOCX export.",
            )

    except Exception as e:
        # unexpected -- log full detail, but keep the user-facing message clean
        logger.exception("Unexpected error parsing resume for %s", candidate_id)
        db.mark_upload_status(upload_id, "failed", str(e))
        return ResumeParseResult(ok=False, error="Something went wrong parsing your resume.")


def _try_backup_to_b2(upload_id: int, file_path: str, candidate_id: str):
    """B2 upload is best-effort backup, not on the critical path -- a
    parsed profile is already saved in sqlite by the time this runs, so
    a B2 failure here (bad credentials, network blip) should never fail
    the user's upload. Logged, not raised."""
    try:
        key = b2.upload_resume(file_path, candidate_id)
        db.set_b2_key(upload_id, key)
        logger.info("B2 backup complete: %s", key)
    except Exception as e:
        # Deliberately broad -- B2 backup must NEVER be able to fail a
        # resume parse that already succeeded. Anything unexpected here
        # (bad creds, network blip, boto3 quirks) gets logged, not raised.
        logger.warning("B2 backup skipped for upload %s: %s", upload_id, e)