"""
invite_service.py — Staff invite email delivery.

Uses smtplib with the SMTP_* settings from .env.  If credentials are not
configured the function raises so the caller can swallow the error gracefully
(invite is created in DB regardless; admin can copy the link manually).
"""
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import settings

logger = logging.getLogger(__name__)


def send_invite_email(to_email: str, token: str, clinic_name: str, invited_by: str) -> None:
    """Send a staff invitation email with the one-time accept link.

    Raises on missing SMTP config or send failure — callers should catch.
    """
    if not getattr(settings, "SMTP_HOST", None) or not getattr(settings, "SMTP_USER", None):
        logger.warning("SMTP not configured — skipping invite email for %s", to_email)
        raise RuntimeError("SMTP not configured")

    base_url = getattr(settings, "BASE_URL", "http://localhost:8000").rstrip("/")
    accept_url = f"{base_url}/clinic/invite/{token}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"You're invited to join {clinic_name} on Nivora"
    msg["From"]    = getattr(settings, "SMTP_FROM", settings.SMTP_USER)
    msg["To"]      = to_email

    text_body = (
        f"Hi,\n\n"
        f"{invited_by} has invited you to join {clinic_name} as a staff member on Nivora.\n\n"
        f"Click the link below to set up your account (valid for 7 days):\n"
        f"{accept_url}\n\n"
        f"If you didn't expect this invite, you can safely ignore this email.\n\n"
        f"— Nivora"
    )
    html_body = f"""
    <div style="font-family:Inter,sans-serif;max-width:480px;margin:40px auto;color:#111">
      <h2 style="font-size:20px;margin-bottom:8px">You're invited to {clinic_name}</h2>
      <p style="color:#555">{invited_by} has invited you to join as a staff member on Nivora.</p>
      <a href="{accept_url}"
         style="display:inline-block;margin-top:16px;padding:12px 24px;
                background:#111;color:#fff;text-decoration:none;border-radius:8px;font-size:14px">
        Accept Invite
      </a>
      <p style="margin-top:24px;font-size:12px;color:#999">
        Link expires in 7 days. If you didn't expect this, ignore this email.
      </p>
    </div>
    """

    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    port = int(getattr(settings, "SMTP_PORT", 587))
    with smtplib.SMTP(settings.SMTP_HOST, port) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        smtp.sendmail(msg["From"], [to_email], msg.as_string())

    logger.info("Invite email sent to %s for clinic %s", to_email, clinic_name)
