"""
Thin SMTP wrapper. Used for two genuinely different purposes by
dispatch.py: sending a drafted message directly to an HR contact (only
when a real email was found stated in the posting), and sending the
candidate themselves a digest email with an apply link when no direct
contact exists. Both go through this one function -- the caller decides
who the recipient is, this module just knows how to send.
"""

import logging
import smtplib
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from email.utils import make_msgid

from config import settings

logger = logging.getLogger(__name__)


class EmailConfigError(Exception):
    pass


class EmailSendError(Exception):
    pass


def send_email(to_email: str, subject: str, body: str, attachment_path: str = None) -> str:
    """Returns the Message-ID we set on the outgoing email. reply_watcher
    matches incoming replies back to a lead via this ID (email clients
    echo it back in the In-Reply-To/References headers), so it has to be
    generated here and stored by the caller -- not an afterthought.

    attachment_path is optional and best-effort -- a missing/unreadable
    file logs a warning and the email still sends without it, since a
    cold email without a resume attached is still better than no email
    at all (dispatch.py's digest-to-self path never needs an attachment
    in the first place)."""
    if not (settings.SMTP_USER and settings.SMTP_PASSWORD):
        raise EmailConfigError(
            "SMTP_USER / SMTP_PASSWORD not set in .env -- for Gmail, use an "
            "App Password (not your real password): "
            "https://myaccount.google.com/apppasswords"
        )
    if not to_email:
        raise EmailConfigError("No recipient email provided")

    message_id = make_msgid()

    msg = MIMEMultipart()
    msg["From"] = settings.SMTP_FROM_EMAIL
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg.attach(MIMEText(body, "plain"))

    if attachment_path:
        _attach_file(msg, attachment_path)

    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20) as server:
            server.starttls()
            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.send_message(msg)
        logger.info("Email sent to %s: %s (Message-ID: %s, attachment: %s)",
                    to_email, subject, message_id, bool(attachment_path))
        return message_id
    except smtplib.SMTPException as e:
        raise EmailSendError(f"Failed to send email to {to_email}: {e}")


def _attach_file(msg: MIMEMultipart, file_path: str):
    path = Path(file_path)
    if not path.exists():
        logger.warning("Attachment not found at %s -- sending without it", file_path)
        return
    try:
        with open(path, "rb") as f:
            part = MIMEApplication(f.read(), Name=path.name)
        part["Content-Disposition"] = f'attachment; filename="{path.name}"'
        msg.attach(part)
    except OSError as e:
        # Best-effort -- a broken attachment must never block the whole send
        logger.warning("Failed to attach %s (%s) -- sending without it", file_path, e)