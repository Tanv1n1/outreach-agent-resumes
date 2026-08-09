"""
Backblaze B2 access via its S3-compatible API (boto3) -- avoids pulling
in the separate b2sdk dependency for what is, functionally, three calls:
upload, download, and a presigned-url generator.

Resume files land here after upload; sqlite only ever stores the B2 key,
never the file bytes.
"""

import logging
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from boto3.exceptions import S3UploadFailedError

from config import settings

logger = logging.getLogger(__name__)


def _client():
    if not (settings.B2_ENDPOINT_URL and settings.B2_KEY_ID and settings.B2_APPLICATION_KEY):
        raise B2ConfigError(
            "B2 credentials missing. Set B2_ENDPOINT_URL, B2_KEY_ID, "
            "B2_APPLICATION_KEY in your .env"
        )
    return boto3.client(
        "s3",
        endpoint_url=settings.B2_ENDPOINT_URL,
        aws_access_key_id=settings.B2_KEY_ID,
        aws_secret_access_key=settings.B2_APPLICATION_KEY,
    )


def upload_resume(local_path: str, candidate_id: str) -> str:
    """Uploads a resume file to B2, returns the object key.
    Key is namespaced by candidate_id so re-uploads/re-parses don't collide."""
    filename = Path(local_path).name
    key = f"resumes/{candidate_id}/{filename}"

    try:
        _client().upload_file(local_path, settings.B2_BUCKET_NAME, key)
        logger.info("Uploaded %s to B2 as %s", local_path, key)
        return key
    except (ClientError, S3UploadFailedError) as e:
        # Non-fatal by design -- the local file still exists and the
        # pipeline can proceed on it. B2 is durability/backup, not the
        # only copy. main.py logs this but doesn't block parsing.
        # NOTE: boto3's managed upload_file() wraps the underlying
        # ClientError in its own S3UploadFailedError, so both must be
        # caught here or credential/permission errors escape this
        # try block entirely and take down the caller. (Fixed after
        # exactly this happening on a bad B2 key.)
        raise B2UploadError(f"B2 upload failed: {e}")


def download_resume(b2_key: str, dest_path: str) -> str:
    try:
        _client().download_file(settings.B2_BUCKET_NAME, b2_key, dest_path)
        return dest_path
    except ClientError as e:
        raise B2UploadError(f"B2 download failed for key {b2_key}: {e}")


class B2ConfigError(Exception):
    pass


class B2UploadError(Exception):
    pass