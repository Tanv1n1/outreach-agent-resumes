"""
Job board clients. Both are legitimate APIs, not scrapers -- Adzuna
requires a free API key (https://developer.adzuna.com), RemoteOK's JSON
feed is publicly documented and requires no auth.

Deliberately NOT scraping LinkedIn/Naukri/Indeed here -- against their
ToS and a real ban/legal risk. If more coverage is needed later, the
right next additions are Greenhouse/Lever public job-board JSON APIs
(most startups already expose these, no scraping involved), added in
company_finder.py rather than here since they're per-company, not a
blanket search.

Every function returns list[JobLead] -- the caller (finder.py) doesn't
need to know which source it came from beyond what's already on the
JobLead itself.
"""

import logging
import requests

from core.models import JobLead
from config import settings

logger = logging.getLogger(__name__)

ADZUNA_BASE_URL = "https://api.adzuna.com/v1/api/jobs"
REMOTEOK_URL = "https://remoteok.com/api"


def search_adzuna(query: str, location: str = "", max_results: int = 20) -> list[JobLead]:
    if not (settings.ADZUNA_APP_ID and settings.ADZUNA_APP_KEY):
        logger.warning("Adzuna credentials not set -- skipping Adzuna search for '%s'", query)
        return []

    url = f"{ADZUNA_BASE_URL}/{settings.ADZUNA_COUNTRY}/search/1"
    params = {
        "app_id": settings.ADZUNA_APP_ID,
        "app_key": settings.ADZUNA_APP_KEY,
        "what": query,
        "where": location,
        "results_per_page": max_results,
        "content-type": "application/json",
    }

    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Adzuna search failed for '%s': %s", query, e)
        return []

    leads = []
    for job in resp.json().get("results", []):
        leads.append(JobLead(
            source="adzuna",
            external_id=str(job.get("id", "")),
            title=job.get("title", "").strip(),
            company=(job.get("company") or {}).get("display_name", "Unknown"),
            location=(job.get("location") or {}).get("display_name", ""),
            description=job.get("description", ""),
            url=job.get("redirect_url", ""),
            posted_at=job.get("created", ""),
        ))
    logger.info("Adzuna: %d results for '%s'", len(leads), query)
    return leads


def _fetch_remoteok_feed() -> list[dict]:
    """Fetches RemoteOK's full recent-postings feed once. Filtered
    per-query by _filter_remoteok_jobs rather than re-fetched, since the
    API has no server-side query param and hitting it once per role
    query (7+ times in a single lead_finder run) would be wasteful."""
    try:
        resp = requests.get(
            REMOTEOK_URL,
            headers={"User-Agent": "outreach-agent/1.0"},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("RemoteOK fetch failed: %s", e)
        return []

    jobs = resp.json()
    if jobs and "legal" in jobs[0]:  # first element is feed metadata, not a job
        jobs = jobs[1:]
    return jobs


def search_remoteok(query: str, max_results: int = 20, _feed_cache: list[dict] = None) -> list[JobLead]:
    jobs = _feed_cache if _feed_cache is not None else _fetch_remoteok_feed()

    query_lower = query.lower()
    leads = []
    for job in jobs:
        haystack = " ".join([
            job.get("position", ""),
            job.get("description", ""),
            " ".join(job.get("tags", []) or []),
        ]).lower()
        if query_lower not in haystack:
            continue

        leads.append(JobLead(
            source="remoteok",
            external_id=str(job.get("id", "")),
            title=job.get("position", "").strip(),
            company=job.get("company", "Unknown"),
            location=job.get("location", "Remote"),
            description=job.get("description", ""),
            url=job.get("url", ""),
            posted_at=job.get("date", ""),
        ))
        if len(leads) >= max_results:
            break

    logger.info("RemoteOK: %d results for '%s'", len(leads), query)
    return leads


def search_all_boards(queries: list[str], location: str = "", max_results_per_board: int = 20) -> list[JobLead]:
    """Queries every configured board across a list of role queries.
    Failures in one board don't block the others (each search_* function
    already catches its own request errors). RemoteOK's feed is fetched
    once and reused across all queries, not re-fetched per query."""
    leads = []
    remoteok_feed = _fetch_remoteok_feed()

    for query in queries:
        leads.extend(search_adzuna(query, location, max_results_per_board))
        leads.extend(search_remoteok(query, max_results_per_board, _feed_cache=remoteok_feed))
    return leads
