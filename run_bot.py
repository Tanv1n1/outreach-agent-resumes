"""
Starts the Telegram bot and keeps it running, listening for your
commands and button taps. Unlike run_parser.py / run_lead_finder.py,
this doesn't exit -- leave it running in a terminal (or as a background
service later) while you use the bot.

Usage:
    python run_bot.py

Then in Telegram: find your bot (the one @BotFather gave you a token
for), send /start, then /newleads <candidate_id> to pull in whatever
lead_finder has found so far.
"""

import logging
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  |  %(message)s",
    datefmt="%H:%M:%S",
)

from core.db import init_db
from services.notifier.telegram_bot import build_app

if __name__ == "__main__":
    init_db()
    app = build_app()
    print("Bot running. Message it /start on Telegram, then Ctrl+C here to stop.")
    app.run_polling()