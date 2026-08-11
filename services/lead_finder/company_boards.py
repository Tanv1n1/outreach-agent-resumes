"""
Pulls open roles directly from company career pages via their applicant
tracking system's public JSON API -- Greenhouse and Lever both expose
these with no auth required, meant for exactly this kind of consumption
(their own "embed your jobs elsewhere" feature). Zero scraping, zero
ToS risk -- this is the intended, documented way to read these boards.

This is also the natural home for "companies not currently hiring but
worth reaching out to" from the original spec: even a company with an
empty board result here is a legitimate cold-outreach target, just
without an open-role hook to reference in the message.

Coverage is bounded by config/watched_companies.json -- there's no
public directory of "every company using Greenhouse", so this is a
user-curated watchlist, not blanket discovery. Add companies you're
actually interested in; it grows in value the more you add.
"""

import json
import logging
from pathlib import Path

import requests

from core.models import JobLead

logger = logging.getLogger(__name__)

GREENHOUSE_JOBS_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
LEVER_JOBS_URL = "https://api.lever.co/v0/postings/{token}?mode=json"

WATCHED_COMPANIES_PATH = Path(__file__).parent.parent.parent / "config" / "watched_companies.json"


def load_watched_companies() -> dict:
    if not WATCHED_COMPANIES_PATH.exists():
        logger.warning("watched_companies.json not found at %s -- skipping company boards", WATCHED_COMPANIES_PATH)
        return {"greenhouse": [], "lever": []}
    try:
        with open(WATCHED_COMPANIES_PATH) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        # Non-fatal by design -- a broken/empty config file here shouldn't
        # take down job board search results that already succeeded.
        # Common cause: the file is empty (0 bytes) -- happens if it got
        # overwritten/truncated during a copy/save.
        logger.warning(
            "watched_companies.json is empty or invalid JSON (%s) -- "
            "skipping company boards this run. Check the file isn't empty.", e,
        )
        return {"greenhouse": [], "lever": []}
    return {"greenhouse": data.get("greenhouse", []), "lever": data.get("lever", [])}


def fetch_greenhouse_jobs(token: str) -> list[JobLead]:
    url = GREENHOUSE_JOBS_URL.format(token=token)
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 404:
            logger.warning("Greenhouse: no board found for token '%s' -- check the slug", token)
            return []
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Greenhouse fetch failed for '%s': %s", token, e)
        return []

    leads = []
    for job in resp.json().get("jobs", []):
        leads.append(JobLead(
            source="greenhouse",
            external_id=str(job.get("id", "")),
            title=job.get("title", "").strip(),
            company=token,   # Greenhouse's job payload doesn't repeat company name -- use the token
            location=(job.get("location") or {}).get("name", ""),
            description=job.get("content", ""),   # HTML -- left as-is, scorer/regex work fine on raw text
            url=job.get("absolute_url", ""),
            posted_at=job.get("updated_at", ""),
        ))
    logger.info("Greenhouse (%s): %d open roles", token, len(leads))
    return leads


def fetch_lever_jobs(token: str) -> list[JobLead]:
    url = LEVER_JOBS_URL.format(token=token)
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 404:
            logger.warning("Lever: no board found for token '%s' -- check the slug", token)
            return []
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Lever fetch failed for '%s': %s", token, e)
        return []

    leads = []
    for job in resp.json():
        categories = job.get("categories", {}) or {}
        leads.append(JobLead(
            source="lever",
            external_id=str(job.get("id", "")),
            title=job.get("text", "").strip(),
            company=token,
            location=categories.get("location", ""),
            description=job.get("descriptionPlain", "") or job.get("description", ""),
            url=job.get("hostedUrl", ""),
            posted_at=str(job.get("createdAt", "")),
        ))
    logger.info("Lever (%s): %d open roles", token, len(leads))
    return leads


def search_watched_companies() -> list[JobLead]:
    """Pulls every open role across all companies in watched_companies.json.
    No query filtering here -- returns everything, scorer.py decides what's
    actually relevant. Filtering at the source would risk missing a role
    with an unexpected title that's still a great skill match."""
    watched = load_watched_companies()
    leads = []

    for token in watched["greenhouse"]:
        leads.extend(fetch_greenhouse_jobs(token))
    for token in watched["lever"]:
        leads.extend(fetch_lever_jobs(token))

    logger.info(
        "Company boards: %d total roles across %d companies",
        len(leads), len(watched["greenhouse"]) + len(watched["lever"]),
    )
    return leads