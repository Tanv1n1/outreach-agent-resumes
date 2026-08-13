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
from services.lead_finder.company_boards import search_watched_companies
from services.lead_finder.scorer import score_lead

logger = logging.getLogger(__name__)

MIN_SCORE_TO_KEEP = 25.0   # below this, a lead is noise -- not worth showing for approval


def find_leads(
    profile: CandidateProfile,
    role_queries: list[str] = None,
    location: str = "",
    max_per_query: int = 15,
    include_company_boards: bool = True,
) -> list[ScoredLead]:
    """Searches job boards (Adzuna/RemoteOK/WeWorkRemotely/Jobicy) across
    role_queries, plus every company in config/watched_companies.json
    (Greenhouse/Lever), scores everything against the profile, persists
    new ones, returns everything above MIN_SCORE_TO_KEEP sorted best-first.

    Safe to re-run -- db.save_lead() dedupes on (source, external_id,
    candidate_id), so already-seen postings won't be re-inserted or
    re-shown for approval.
    """
    queries = role_queries or settings.DEFAULT_ROLE_QUERIES
    logger.info("Searching %d role queries across job boards...", len(queries))

    raw_leads = search_all_boards(queries, location=location, max_results_per_board=max_per_query)
    logger.info("Found %d raw leads from job boards", len(raw_leads))

    if include_company_boards:
        company_leads = search_watched_companies()
        logger.info("Found %d raw leads from watched company boards", len(company_leads))
        raw_leads.extend(company_leads)

    # Dedupe BEFORE scoring, not just at the DB layer -- the same posting
    # can legitimately match multiple role queries (e.g. "Product Manager"
    # and "Program Manager" both hitting one WeWorkRemotely listing), which
    # without this produces the same lead scored and shown twice in one run
    # even though db.save_lead()'s UNIQUE constraint correctly rejects the
    # second insert. Confirmed bug from a real run: Reddit's GPM listing
    # appeared twice, identical, in the results.
    seen = set()
    deduped_leads = []
    for lead in raw_leads:
        key = (lead.source, lead.external_id or lead.url)
        if key in seen:
            continue
        seen.add(key)
        deduped_leads.append(lead)
    if len(deduped_leads) < len(raw_leads):
        logger.info("Deduped %d repeat leads (matched by multiple role queries)",
                     len(raw_leads) - len(deduped_leads))
    raw_leads = deduped_leads

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