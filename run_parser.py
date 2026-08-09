"""
Standalone runner to test resume parsing before the Telegram bot exists.

Usage:
    python run_parser.py path/to/resume.pdf
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

from core.db import init_db
from services.resume_parser.parser import parse_resume

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python run_parser.py path/to/resume.pdf")
        sys.exit(1)

    init_db()
    result = parse_resume(sys.argv[1])

    if result.ok:
        print(f"\nParsed candidate_id: {result.profile.candidate_id}")
        if result.profile.parse_warnings:
            print("Warnings:")
            for w in result.profile.parse_warnings:
                print(f"  - {w}")
        print("\n" + result.profile.to_json())
    else:
        print(f"\nFailed: {result.error}")
        sys.exit(1)