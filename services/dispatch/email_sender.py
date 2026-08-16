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
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from config import settings

logger = logging.getLogger(__name__)


class EmailConfigError(Exception):
    pass


class EmailSendError(Exception):
    pass


def send_email(to_email: str, subject: str, body: str) -> None:
    if not (settings.SMTP_USER and settings.SMTP_PASSWORD):
        raise EmailConfigError(
            "SMTP_USER / SMTP_PASSWORD not set in .env -- for Gmail, use an "
            "App Password (not your real password): "
            "https://myaccount.google.com/apppasswords"
        )
    if not to_email:
        raise EmailConfigError("No recipient email provided")

    msg = MIMEMultipart()
    msg["From"] = settings.SMTP_FROM_EMAIL
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20) as server:
            server.starttls()
            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.send_message(msg)
        logger.info("Email sent to %s: %s", to_email, subject)
    except smtplib.SMTPException as e:
        raise EmailSendError(f"Failed to send email to {to_email}: {e}")