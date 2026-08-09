"""
Job board clients. All sources use legitimate APIs, RSS feeds, or public JSON
endpoints -- no web scraping involved.

Every function returns list[JobLead] so caller (finder.py) gets uniform
data structures.
"""

import logging
import requests
import xml.etree.ElementTree as ET

from core.models import JobLead
from config import settings

logger = logging.getLogger(__name__)

ADZUNA_BASE_URL = "https://api.adzuna.com/v1/api/jobs"
REMOTEOK_URL = "https://remoteok.com/api"
WWR_PRODUCT_RSS = "https://weworkremotely.com/categories/remote-product-jobs.rss"
JOBICY_URL = "https://jobicy.com/api/v2/remote-jobs"


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
    if jobs and "legal" in jobs[0]:
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


def _fetch_wwr_feed() -> list[dict]:
    try:
        resp = requests.get(
            WWR_PRODUCT_RSS,
            headers={"User-Agent": "outreach-agent/1.0"},
            timeout=15
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        items = []
        for item in root.findall("./channel/item"):
            items.append({
                "raw_title": item.findtext("title", ""),
                "link": item.findtext("link", ""),
                "description": item.findtext("description", ""),
                "pubDate": item.findtext("pubDate", "")
            })
        return items
    except Exception as e:
        logger.warning("WeWorkRemotely fetch failed: %s", e)
        return []


def search_weworkremotely(query: str, max_results: int = 20, _feed_cache: list[dict] = None) -> list[JobLead]:
    items = _feed_cache if _feed_cache is not None else _fetch_wwr_feed()
    query_lower = query.lower()
    leads = []

    for item in items:
        haystack = f"{item['raw_title']} {item['description']}".lower()
        if query_lower not in haystack:
            continue

        raw_title = item['raw_title']
        company, title = "Unknown", raw_title
        if " is hiring a " in raw_title:
            company, title = raw_title.split(" is hiring a ", 1)
        elif ":" in raw_title:
            company, title = raw_title.split(":", 1)

        leads.append(JobLead(
            source="weworkremotely",
            external_id=item['link'],
            title=title.strip(),
            company=company.strip(),
            location="Remote",
            description=item['description'],
            url=item['link'],
            posted_at=item['pubDate'],
        ))
        if len(leads) >= max_results:
            break

    logger.info("WeWorkRemotely: %d results for '%s'", len(leads), query)
    return leads


def _fetch_jobicy_feed() -> list[dict]:
    try:
        resp = requests.get(
            JOBICY_URL,
            params={"count": 50},
            headers={"User-Agent": "outreach-agent/1.0"},
            timeout=15
        )
        resp.raise_for_status()
        return resp.json().get("jobs", [])
    except Exception as e:
        logger.warning("Jobicy fetch failed: %s", e)
        return []


def search_jobicy(query: str, max_results: int = 20, _feed_cache: list[dict] = None) -> list[JobLead]:
    jobs = _feed_cache if _feed_cache is not None else _fetch_jobicy_feed()
    query_lower = query.lower()
    leads = []

    for job in jobs:
        haystack = " ".join([
            job.get("jobTitle", ""),
            job.get("jobExcerpt", ""),
            job.get("companyName", ""),
            job.get("jobCategory", "")
        ]).lower()
        if query_lower not in haystack:
            continue

        leads.append(JobLead(
            source="jobicy",
            external_id=str(job.get("id", "")),
            title=job.get("jobTitle", "").strip(),
            company=job.get("companyName", "Unknown"),
            location=job.get("jobGeo", "Remote"),
            description=job.get("jobExcerpt", ""),
            url=job.get("url", ""),
            posted_at=job.get("pubDate", ""),
        ))
        if len(leads) >= max_results:
            break

    logger.info("Jobicy: %d results for '%s'", len(leads), query)
    return leads


def search_all_boards(queries: list[str], location: str = "", max_results_per_board: int = 20) -> list[JobLead]:
    leads = []
    remoteok_feed = _fetch_remoteok_feed()
    wwr_feed = _fetch_wwr_feed()
    jobicy_feed = _fetch_jobicy_feed()

    for query in queries:
        leads.extend(search_adzuna(query, location, max_results_per_board))
        leads.extend(search_remoteok(query, max_results_per_board, _feed_cache=remoteok_feed))
        leads.extend(search_weworkremotely(query, max_results_per_board, _feed_cache=wwr_feed))
        leads.extend(search_jobicy(query, max_results_per_board, _feed_cache=jobicy_feed))

    return leads