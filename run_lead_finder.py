"""
Standalone runner to test lead_finder against a profile already parsed
and saved by run_parser.py.

Usage:
    python run_lead_finder.py <candidate_id> [location]
"""

import sys
import logging
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  |  %(message)s",
    datefmt="%H:%M:%S",
)

logger = logging.getLogger(__name__)

from core.db import init_db, get_profile
from core.models import CandidateProfile
from services.lead_finder.finder import find_leads

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python run_lead_finder.py <candidate_id> [location]", flush=True)
        sys.exit(1)

    candidate_id = sys.argv[1]
    location = sys.argv[2] if len(sys.argv) > 2 else ""

    logger.info("Initializing database connection...")
    init_db()

    logger.info(f"Fetching profile for candidate_id: {candidate_id}")
    profile_dict = get_profile(candidate_id)
    if not profile_dict:
        print(f"No profile found for candidate_id={candidate_id}. Run run_parser.py first.", flush=True)
        sys.exit(1)

    profile = CandidateProfile.from_dict(profile_dict)
    print(f"\nLoaded profile: {profile.full_name} ({len(profile.skills)} skills)\n", flush=True)

    logger.info("Triggering find_leads service (fetching job boards + embedding comparison)...")
    
    try:
        leads = find_leads(profile, location=location)
    except Exception as e:
        logger.exception(f"Error occurred inside find_leads: {e}")
        sys.exit(1)

    logger.info(f"find_leads completed. Returned {len(leads) if leads else 0} results.")

    if not leads:
        print("\nNo leads found above the score threshold. "
              "Check ADZUNA_APP_ID/ADZUNA_APP_KEY are set in .env, "
              "or that RemoteOK has relevant postings right now.", flush=True)
        sys.exit(0)

    print(f"\n{'='*70}\nTop {min(len(leads), 10)} leads:\n{'='*70}", flush=True)
    for i, sl in enumerate(leads[:10], 1):
        print(f"\n{i}. [{sl.score}] {sl.lead.title} @ {sl.lead.company} "
              f"({sl.lead.source}, {sl.lead.location or 'n/a'})", flush=True)
        for r in sl.reasons:
            print(f"     - {r}", flush=True)
        if sl.lead.url:
            print(f"     {sl.lead.url}", flush=True)