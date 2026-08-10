"""
Add a single job lead you found yourself (LinkedIn, Naukri, Indeed, etc.)
by browsing normally -- pastes it through the same scorer as everything
else, then saves it if it's worth keeping.

Usage (URL, best-effort auto-fetch):
    python run_manual_lead.py <candidate_id> --url "https://..."

Usage (manual paste, works even when the URL is behind a login wall --
expect to use this mode most often for LinkedIn/Naukri):
    python run_manual_lead.py <candidate_id> --title "Product Manager" \\
        --company "Acme Inc" --description "Full JD text here..." \\
        --url "https://... (optional, just for reference)"
"""

import sys
import argparse
import logging
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)-7s | %(message)s")

from core.db import init_db, get_profile, save_lead
from core.models import CandidateProfile
from services.lead_finder.manual_lead import add_manual_lead

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("candidate_id")
    parser.add_argument("--url", default="")
    parser.add_argument("--title", default="")
    parser.add_argument("--company", default="")
    parser.add_argument("--description", default="")
    parser.add_argument("--location", default="")
    args = parser.parse_args()

    init_db()
    profile_dict = get_profile(args.candidate_id)
    if not profile_dict:
        print(f"No profile found for candidate_id={args.candidate_id}. Run run_parser.py first.")
        sys.exit(1)

    profile = CandidateProfile.from_dict(profile_dict)

    try:
        scored = add_manual_lead(
            profile, url=args.url, title=args.title, company=args.company,
            description=args.description, location=args.location,
        )
    except ValueError as e:
        print(f"\n{e}")
        sys.exit(1)

    print(f"\nScored: {scored.score}  --  {scored.lead.title} @ {scored.lead.company}")
    for r in scored.reasons:
        print(f"  - {r}")

    lead_id = save_lead(args.candidate_id, scored)
    if lead_id:
        print(f"\nSaved as lead #{lead_id}")
    else:
        print("\nAlready in the database (duplicate) -- not re-saved.")