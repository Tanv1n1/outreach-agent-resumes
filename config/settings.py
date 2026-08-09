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

# --- Local paths ---
LOCAL_UPLOAD_DIR = os.environ.get("LOCAL_UPLOAD_DIR", "data/uploads")  # scratch space before B2 upload
