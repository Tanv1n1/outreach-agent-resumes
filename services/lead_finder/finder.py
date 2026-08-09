"""
Public entry point for lead_finder. Everything else should import
find_leads from HERE, not reach into job_boards/scorer directly --
same pattern as resume_parser/parser.py.
"""

import logging

from core import db
from core.models import CandidateProfile, ScoredLead
from config import settings
from services.lead_finder.job_boards import search_all_boards
from services.lead_finder.scorer import score_lead

logger = logging.getLogger(__name__)

MIN_SCORE_TO_KEEP = 25.0   # below this, a lead is noise -- not worth showing for approval


def find_leads(
    profile: CandidateProfile,
    role_queries: list[str] = None,
    location: str = "",
    max_per_query: int = 15,
) -> list[ScoredLead]:
    """Searches all configured job boards across role_queries (defaults to
    settings.DEFAULT_ROLE_QUERIES for broad matching), scores every result
    against the profile, persists new ones, and returns everything above
    MIN_SCORE_TO_KEEP sorted best-first.

    Safe to re-run -- db.save_lead() dedupes on (source, external_id,
    candidate_id), so already-seen postings won't be re-inserted or
    re-shown for approval.
    """
    queries = role_queries or settings.DEFAULT_ROLE_QUERIES
    logger.info("Searching %d role queries across job boards...", len(queries))

    raw_leads = search_all_boards(queries, location=location, max_results_per_board=max_per_query)
    logger.info("Found %d raw leads before scoring/dedup", len(raw_leads))

    scored: list[ScoredLead] = []
    new_count = 0
    for lead in raw_leads:
        result = score_lead(profile, lead)
        if result.score < MIN_SCORE_TO_KEEP:
            continue

        lead_id = db.save_lead(profile.candidate_id, result)
        if lead_id is not None:
            new_count += 1
        scored.append(result)

    scored.sort(key=lambda s: s.score, reverse=True)
    logger.info(
        "%d leads scored above %.0f (%d newly saved, rest already seen)",
        len(scored), MIN_SCORE_TO_KEEP, new_count,
    )
    return scored
