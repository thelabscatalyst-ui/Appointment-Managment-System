"""
invite_service.py — clinic doctor-invite email delivery.

Rewritten onto services.email_service (Resend). The previous implementation
used smtplib with SMTP_* settings that were never declared on Settings, so
`getattr(settings, "SMTP_HOST", None)` was always None and every call raised
RuntimeError — swallowed by the caller's bare `except`. No invite email has
ever actually been delivered.

It also built the wrong URL (`/clinic/invite/{token}`); the real accept route
is `/clinic/doctor-invite/{token}` (routers/clinic.py:288), so any link that
had gone out would have 404'd.
"""
import logging

from config import settings
from services.email_service import send_email, render_email, button

logger = logging.getLogger(__name__)

# Matches ClinicDoctorInvite.expires_at set in routers/clinic.py
INVITE_VALID_DAYS = 7


def build_invite_url(token: str) -> str:
    """Public accept link for a clinic doctor invite.

    Must match the route in routers/clinic.py — the router carries a
    `/clinic` prefix and the path is `/doctor-invite/{token}`.
    """
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    return f"{base}/clinic/doctor-invite/{token}"


def send_invite_email(
    to_email: str, token: str, clinic_name: str, invited_by: str
) -> tuple[bool, str]:
    """Send a clinic invitation with its one-time accept link.

    Never raises — returns (ok, detail). The invite row already exists in the
    DB regardless, so a mail failure just means the owner has to share the
    link manually.
    """
    accept_url = build_invite_url(token)

    body = f"""
      <h2 style="margin:0 0 12px;font-size:20px;font-weight:700;color:#1a1410;">
        You've been invited to {clinic_name}
      </h2>
      <p style="margin:0 0 16px;color:#5c5148;">
        <strong>{invited_by}</strong> has invited you to join
        <strong>{clinic_name}</strong> on Nivora, where you'll be able to manage
        your own appointments, patients, and billing under the clinic's plan.
      </p>
      {button(accept_url, "Accept invitation")}
      <p style="margin:18px 0 0;font-size:13px;color:#8a7f74;">
        This invitation expires in {INVITE_VALID_DAYS} days. If the button
        doesn't work, paste this link into your browser:<br>
        <span style="word-break:break-all;color:#6b4a28;">{accept_url}</span>
      </p>
    """

    ok, detail = send_email(
        to=to_email,
        subject=f"You're invited to join {clinic_name} on Nivora",
        html=render_email(
            body,
            footer_html=(
                f"You received this because {invited_by} invited you to "
                f"{clinic_name} on Nivora. If you weren't expecting it, you "
                f"can safely ignore this email."
            ),
        ),
    )

    if ok:
        logger.info("Invite email sent to %s for clinic %s", to_email, clinic_name)
    else:
        logger.warning(
            "Invite email NOT sent to %s for clinic %s: %s — share the link manually: %s",
            to_email, clinic_name, detail, accept_url,
        )
    return ok, detail
