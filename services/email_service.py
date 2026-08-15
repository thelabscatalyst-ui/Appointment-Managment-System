"""
email_service.py — transactional email via Resend.

One generic sender used by every outbound email in the app: verification
codes, password resets, and clinic invites. Before this existed, the only
mail path was `invite_service` reading SMTP_* settings that were never
declared on Settings, so it raised on every call and no email was ever sent.

Contract (mirrors services/notification_service.send_whatsapp):

  * `send_email` NEVER raises. It returns (ok, detail). A mail outage must
    not 500 a registration or a bill.
  * Unconfigured (no RESEND_API_KEY) is a normal state, not an error — it
    logs at WARNING, returns (False, "not configured"), and in DEBUG mode
    prints the message so local dev can read verification codes without a
    Resend account.
"""
import logging
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)

# Wrapped in the shared shell so every email looks like it came from Med Track.
# Inline styles only — Gmail/Outlook strip <style> blocks.
_LAYOUT = """\
<div style="background:#f5f2ec;padding:32px 16px;font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <div style="max-width:480px;margin:0 auto;background:#ffffff;border:1px solid #e4dbcd;border-radius:16px;overflow:hidden;">
    <div style="background:#2e1e0c;padding:20px 28px;">
      <span style="color:#f5ead8;font-size:20px;font-weight:700;letter-spacing:-0.2px;">Med Track</span>
    </div>
    <div style="padding:28px;color:#1a1410;font-size:15px;line-height:1.6;">
      {body}
    </div>
    <div style="padding:16px 28px 24px;border-top:1px solid #efe8dc;color:#8a7f74;font-size:12px;line-height:1.5;">
      {footer}
    </div>
  </div>
</div>"""

_DEFAULT_FOOTER = (
    "You're receiving this because someone used this address to sign up for "
    "Med Track, clinic management software for doctors in India. "
    "If that wasn't you, you can safely ignore this email."
)


def render_email(body_html: str, footer_html: str = _DEFAULT_FOOTER) -> str:
    """Wrap message-specific HTML in the shared Med Track shell."""
    return _LAYOUT.format(body=body_html, footer=footer_html)


def button(url: str, label: str) -> str:
    """A call-to-action button. Table-free so it survives most mail clients."""
    return (
        f'<a href="{url}" style="display:inline-block;margin:8px 0 4px;'
        f'padding:12px 26px;background:#6b4a28;color:#f5ead8;text-decoration:none;'
        f'border-radius:10px;font-size:15px;font-weight:600;">{label}</a>'
    )


def code_block(code: str) -> str:
    """Large monospaced display for a one-time verification code."""
    return (
        f'<div style="margin:18px 0;padding:16px;background:#f5f2ec;'
        f'border:1px solid #e4dbcd;border-radius:12px;text-align:center;'
        f'font-family:ui-monospace,SFMono-Regular,Menlo,monospace;'
        f'font-size:30px;font-weight:700;letter-spacing:8px;color:#2e1e0c;">'
        f'{code}</div>'
    )


def send_email(
    to: str,
    subject: str,
    html: str,
    *,
    reply_to: Optional[str] = None,
) -> tuple[bool, str]:
    """Send one transactional email. Never raises.

    Returns (ok, detail) — detail is the provider message id on success, or a
    short reason on failure, suitable for logging.
    """
    recipient = (to or "").strip()
    if not recipient:
        return False, "no recipient"

    if not settings.RESEND_API_KEY:
        # Not an error: local dev and any environment without mail configured.
        logger.warning("Resend not configured — email to %s not sent (%s)", recipient, subject)
        if settings.ENVIRONMENT.lower() != "production":
            # Outside production, surface any link in the body at WARNING.
            # Verification codes appear in the subject line, but a password
            # reset token lives only inside the HTML — logging it at INFO made
            # it invisible under uvicorn's default level, so the reset flow was
            # untestable locally. Never runs in production.
            import re as _re
            links = _re.findall(r'https?://[^\s"\'<>]+', html)
            for link in dict.fromkeys(links):
                logger.warning("[email link] %s", link)
            logger.info("[email preview] to=%s subject=%s\n%s", recipient, subject, html)
        return False, "not configured"

    try:
        import resend
    except ImportError:
        logger.error("resend package not installed — run: pip install resend")
        return False, "resend not installed"

    try:
        resend.api_key = settings.RESEND_API_KEY
        payload = {
            "from": settings.EMAIL_FROM,
            "to": [recipient],
            "subject": subject,
            "html": html,
        }
        effective_reply_to = reply_to or settings.EMAIL_REPLY_TO
        if effective_reply_to:
            payload["reply_to"] = effective_reply_to

        result = resend.Emails.send(payload)
        message_id = (result or {}).get("id", "") if isinstance(result, dict) else ""
        logger.info("Email sent to %s (%s) id=%s", recipient, subject, message_id)
        return True, message_id or "sent"
    except Exception as exc:
        # Covers auth failures, rate limits, unverified sender domains, and
        # network errors. Callers keep working regardless.
        logger.error("Email send failed to %s (%s): %s", recipient, subject, exc)
        return False, str(exc)


# ------------------------------------------------------------------ #
#  Background-task wrapper                                             #
# ------------------------------------------------------------------ #

def send_email_bg(to: str, subject: str, html: str) -> None:
    """Fire-and-forget variant for FastAPI BackgroundTasks.

    Takes only primitives — never pass ORM objects across the background
    boundary, since the request-scoped session is closed by the time this
    runs (same rule as the send_*_bg wrappers in notification_service).
    """
    send_email(to, subject, html)
