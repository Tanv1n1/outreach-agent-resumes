"""
Ingests a single job lead that the user found themselves by browsing
normally (LinkedIn, Naukri, Indeed, wherever) and pasted in -- a human
looking at a page in their own browser isn't scraping, so this carries
none of the ToS/ban risk that automated bulk scraping of those sites
would. This is the deliberate complement to job_boards.py and
company_boards.py, not a workaround for them.

Two input modes:
  - A URL: best-effort single fetch + light HTML parsing (og:title,
    og:site_name meta tags). Many sites (LinkedIn/Naukri especially)
    block this behind a login wall for non-logged-in requests -- that's
    expected and fine, it just means...
  - Raw pasted text: the user copies the job title/company/description
    text directly (e.g. from a LinkedIn post they're logged into) when
    the URL fetch can't reach it. This is the reliable fallback path,
    not an edge case -- for LinkedIn/Naukri specifically, expect to use
    this more often than the URL fetch.
"""

import logging
import re

import requests

from core.models import JobLead, CandidateProfile
from services.lead_finder.scorer import score_lead

logger = logging.getLogger(__name__)

_META_TITLE_RE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE
)
_META_SITE_RE = re.compile(
    r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE
)
_META_DESC_RE = re.compile(
    r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE
)


def add_manual_lead(
    profile: CandidateProfile,
    url: str = "",
    title: str = "",
    company: str = "",
    description: str = "",
    location: str = "",
):
    """Builds a JobLead from either a URL (best-effort fetch) or fields
    the user typed/pasted directly, then scores it the same way as every
    other source. Returns a ScoredLead -- caller decides whether to save it
    (see run_manual_lead.py for the save-on-approval pattern)."""

    if url and not (title and description):
        fetched = _try_fetch_meta(url)
        title = title or fetched.get("title", "")
        company = company or fetched.get("company", "")
        description = description or fetched.get("description", "")

    if not title:
        raise ValueError(
            "Couldn't extract a title automatically (common for LinkedIn/Naukri "
            "behind a login wall). Pass title= and description= directly -- "
            "copy-paste them from the page you're looking at."
        )

    lead = JobLead(
        source="manual",
        title=title.strip(),
        company=company.strip() or "Unknown",
        location=location,
        description=description,
        url=url,
    )
    return score_lead(profile, lead)


def _try_fetch_meta(url: str) -> dict:
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; outreach-agent research fetch)"},
            timeout=10,
        )
        resp.raise_for_status()
    except Exception as e:
        # Broad on purpose -- this is a best-effort fetch (same principle
        # as B2 backup elsewhere in the codebase). Network calls can throw
        # more than just requests.RequestException (DNS failures, SSL
        # errors, etc.), and none of them should propagate up and break
        # what's otherwise a perfectly fine manual-paste lead.
        logger.info("Manual lead: couldn't fetch %s (%s) -- paste details manually instead", url, e)
        return {}

    html = resp.text
    title_match = _META_TITLE_RE.search(html)
    site_match = _META_SITE_RE.search(html)
    desc_match = _META_DESC_RE.search(html)

    return {
        "title": title_match.group(1) if title_match else "",
        "company": site_match.group(1) if site_match else "",
        "description": desc_match.group(1) if desc_match else "",
    }