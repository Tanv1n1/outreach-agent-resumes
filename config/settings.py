"""
Single place every service reads config from. No service should call
os.environ.get() directly -- if a setting needs to change (swap Ollama
host, rotate B2 keys), this is the one file to touch.
"""

import os

# --- LLM (Groq API -- fast hosted inference) ---
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
# Fallback if the primary model errors out or hits a rate limit -- smaller,
# faster, still solid instruction-following. See extractor.py for retry logic.
GROQ_FALLBACK_MODEL = os.environ.get("GROQ_FALLBACK_MODEL", "openai/gpt-oss-20b")

# --- Backblaze B2 (S3-compatible object storage for resume files) ---
B2_ENDPOINT_URL = os.environ.get("B2_ENDPOINT_URL")          # e.g. https://s3.us-west-004.backblazeb2.com
B2_KEY_ID = os.environ.get("B2_KEY_ID")
B2_APPLICATION_KEY = os.environ.get("B2_APPLICATION_KEY")
B2_BUCKET_NAME = os.environ.get("B2_BUCKET_NAME", "outreach-agent-resumes")

# --- Job boards (lead_finder) ---
ADZUNA_APP_ID = os.environ.get("ADZUNA_APP_ID")
ADZUNA_APP_KEY = os.environ.get("ADZUNA_APP_KEY")
ADZUNA_COUNTRY = os.environ.get("ADZUNA_COUNTRY", "in")   # 'in' = India; Adzuna uses ISO country codes per-endpoint

# Roles searched by default when scoring is set to "broad" mode -- i.e.
# match on skills/domain overlap, not just exact title. Extend this list
# rather than hardcoding elsewhere if new adjacent roles come up.
DEFAULT_ROLE_QUERIES = [
    "Product Manager",
    "Associate Product Manager",
    "Product Owner",
    "Product Operations",
    "Program Manager",
    "Business Analyst",
    "Technical Product Manager",
]

# --- Local paths ---
LOCAL_UPLOAD_DIR = os.environ.get("LOCAL_UPLOAD_DIR", "data/uploads")  # scratch space before B2 upload
