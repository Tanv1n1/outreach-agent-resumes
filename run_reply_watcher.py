"""
Runs a reply check + follow-up draft pass once, then exits. Meant to be
scheduled (Windows Task Scheduler / cron) so replies and follow-ups get
processed even if you're not actively chatting with the bot -- results
still land in Telegram either way, since that's where drafts need your
approval regardless of how they were triggered.

Usage:
    python run_reply_watcher.py <candidate_id>
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

from core.db import init_db, get_profile
from core.models import CandidateProfile
from services.reply_watcher.imap_poll import check_for_replies
from services.reply_watcher.followup import find_leads_needing_followup, draft_followup
from services.notifier.telegram_bot import push_draft_sync

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python run_reply_watcher.py <candidate_id>")
        sys.exit(1)

    candidate_id = sys.argv[1]
    init_db()

    print("Checking for replies...")
    replies = check_for_replies(candidate_id)
    print(f"Found {len(replies)} new repl(y/ies).")
    for r in replies:
        print(f"  Lead #{r['lead_id']} [{r['classification']}]: {r['summary']}")

    print("\nChecking for leads due a follow-up...")
    due = find_leads_needing_followup(candidate_id)
    print(f"{len(due)} lead(s) due.")

    if due:
        profile_dict = get_profile(candidate_id)
        profile = CandidateProfile.from_dict(profile_dict)
        from core import db
        for draft_row in due:
            lead = db.get_lead(draft_row["lead_id"])
            if not lead:
                continue
            followup = draft_followup(profile, lead, draft_row)
            db.save_draft(lead["id"], candidate_id, followup["subject"], followup["body"], followup["channel"])
            db.mark_followup_sent(lead["id"])
            push_draft_sync(lead, followup)
            print(f"  Drafted + pushed follow-up for lead #{lead['id']}: {lead['title']} @ {lead['company']}")

    print("\nDone. Any drafted follow-ups are waiting for your approval in Telegram "
          "(use /newleads or check there directly).")