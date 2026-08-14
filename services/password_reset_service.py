"""
password_reset_service.py — "forgot password" via a one-time emailed link.

Before this, a doctor who forgot their password was locked out permanently:
there was no reset route, template, model, or token anywhere in the app.

Security properties (OWASP ASVS §2.5):

  * Token is 32 bytes from `secrets.token_urlsafe`, stored **hashed**. Only
    the emailed URL carries the plaintext, so a leaked DB can't seize accounts.
  * 30-minute expiry, single use (`consumed_at`).
  * Issuing a new link invalidates all prior unconsumed ones.
  * **No user enumeration**: the request endpoint returns an identical
    response whether or not the address exists.
  * Only sent to a **verified** address — otherwise anyone who registered with
    someone else's email could seize that mailbox's account.
  * On success, `Doctor.token_version` is bumped, killing every existing
    session. A stolen session must not survive the reset meant to end it.
"""
import logging
import secrets
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from config import settings
from database.models import Doctor, PasswordReset
from services.auth_service import hash_password, verify_password
from services.email_service import send_email, render_email, button

logger = logging.getLogger(__name__)

TOKEN_TTL_MINUTES = 30
MAX_LINKS_PER_HOUR = 5


def _build_reset_url(token: str) -> str:
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    return f"{base}/reset-password?token={token}"


def request_reset(db: Session, raw_email: str, *, ip: str | None = None) -> None:
    """Issue a reset link if the address maps to a verified account.

    Returns None in every case — deliberately. The caller shows one fixed
    response, so an attacker cannot tell registered addresses from unknown
    ones, verified from unverified, or rate-limited from not.
    """
    from routers.auth import _normalise_email

    email = _normalise_email(raw_email)
    if not email:
        return

    doctor = db.query(Doctor).filter(Doctor.email == email).first()
    if not doctor:
        logger.info("Password reset requested for unknown address")
        return

    if not doctor.is_active:
        logger.info("Password reset requested for deactivated doctor #%s", doctor.id)
        return

    # Requiring a verified address is what stops this becoming a takeover
    # path: registering with someone else's email must not let you reset it.
    if not doctor.email_verified_at:
        logger.info("Password reset blocked — unverified email for doctor #%s", doctor.id)
        _send_unverified_notice(doctor.email)
        return

    recent = (
        db.query(PasswordReset)
        .filter(
            PasswordReset.doctor_id == doctor.id,
            PasswordReset.created_at > datetime.utcnow() - timedelta(hours=1),
        )
        .count()
    )
    if recent >= MAX_LINKS_PER_HOUR:
        logger.warning("Password reset rate-limited for doctor #%s", doctor.id)
        return

    # Only one live link at a time.
    db.query(PasswordReset).filter(
        PasswordReset.doctor_id == doctor.id,
        PasswordReset.consumed_at.is_(None),
    ).update({"consumed_at": datetime.utcnow()}, synchronize_session=False)

    token = secrets.token_urlsafe(32)
    db.add(PasswordReset(
        doctor_id    = doctor.id,
        token_hash   = hash_password(token),
        expires_at   = datetime.utcnow() + timedelta(minutes=TOKEN_TTL_MINUTES),
        requested_ip = (ip or "")[:64] or None,
    ))
    db.commit()

    _send_reset_email(doctor.email, doctor.name, token)


def _send_reset_email(to_email: str, name: str, token: str) -> None:
    first = (name or "there").strip().split(" ")[0]
    url = _build_reset_url(token)
    body = f"""
      <h2 style="margin:0 0 12px;font-size:20px;font-weight:700;color:#1a1410;">
        Reset your password
      </h2>
      <p style="margin:0 0 16px;color:#5c5148;">
        Hi {first}, use the button below to choose a new Nivora password.
      </p>
      {button(url, "Set a new password")}
      <p style="margin:18px 0 0;font-size:13px;color:#8a7f74;">
        This link expires in {TOKEN_TTL_MINUTES} minutes and can only be used once.
        If the button doesn't work, paste this into your browser:<br>
        <span style="word-break:break-all;color:#6b4a28;">{url}</span>
      </p>
      <p style="margin:14px 0 0;font-size:13px;color:#8a7f74;">
        Didn't request this? Ignore this email — your password stays as it is.
      </p>
    """
    send_email(to=to_email, subject="Reset your Nivora password", html=render_email(body))


def _send_unverified_notice(to_email: str) -> None:
    """Tell an unverified address why no reset link is coming.

    Sent to the address itself, so it leaks nothing to a third party while
    still giving a real doctor a way forward instead of silence.
    """
    body = """
      <h2 style="margin:0 0 12px;font-size:20px;font-weight:700;color:#1a1410;">
        Confirm your email first
      </h2>
      <p style="margin:0 0 12px;color:#5c5148;">
        We received a password reset request for this address, but it hasn't
        been confirmed yet — so we can't reset the password from here.
      </p>
      <p style="margin:0;color:#5c5148;">
        Log in and use the "Confirm your email" prompt, then try again. If you
        can't log in at all, reply to this email and we'll help.
      </p>
    """
    send_email(
        to=to_email,
        subject="About your Nivora password reset",
        html=render_email(body),
    )


def validate_token(db: Session, token: str) -> PasswordReset | None:
    """Return the live reset record for this token, or None.

    Tokens are hashed at rest, so this scans unconsumed, unexpired rows and
    verifies against each. Volume is tiny (they expire in 30 minutes).
    """
    if not token or len(token) < 20:
        return None

    candidates = (
        db.query(PasswordReset)
        .filter(
            PasswordReset.consumed_at.is_(None),
            PasswordReset.expires_at > datetime.utcnow(),
        )
        .order_by(PasswordReset.id.desc())
        .limit(200)
        .all()
    )
    for record in candidates:
        if verify_password(token, record.token_hash):
            return record
    return None


def consume_reset(db: Session, record: PasswordReset, new_password: str) -> tuple[bool, str]:
    """Apply a new password against a validated reset record."""
    doctor = db.query(Doctor).filter(Doctor.id == record.doctor_id).first()
    if not doctor:
        return False, "That account no longer exists."

    from services.password_policy import validate_password
    problems = validate_password(
        new_password,
        email=doctor.email,
        name=doctor.name,
        clinic_name=doctor.clinic_name or "",
    )
    if problems:
        return False, " ".join(problems)

    doctor.password_hash = hash_password(new_password)
    # Kill every existing session — including whoever may have stolen one.
    doctor.token_version = (doctor.token_version or 0) + 1
    record.consumed_at = datetime.utcnow()

    # Any other outstanding link is now void too.
    db.query(PasswordReset).filter(
        PasswordReset.doctor_id == doctor.id,
        PasswordReset.consumed_at.is_(None),
    ).update({"consumed_at": datetime.utcnow()}, synchronize_session=False)
    db.commit()

    logger.info("Password reset completed for doctor #%s", doctor.id)
    _send_changed_notice(doctor.email, doctor.name)
    return True, "Password updated. Please log in."


def _send_changed_notice(to_email: str, name: str) -> None:
    """Confirm the change. If it wasn't them, this is how they find out."""
    first = (name or "there").strip().split(" ")[0]
    body = f"""
      <h2 style="margin:0 0 12px;font-size:20px;font-weight:700;color:#1a1410;">
        Your password was changed
      </h2>
      <p style="margin:0 0 12px;color:#5c5148;">
        Hi {first}, your Nivora password was just reset and you've been signed
        out on all devices.
      </p>
      <p style="margin:0;color:#5c5148;">
        <strong>If this wasn't you</strong>, contact us immediately — someone
        else may have access to your email account.
      </p>
    """
    send_email(to=to_email, subject="Your Nivora password was changed", html=render_email(body))


# ------------------------------------------------------------------ #
#  Background-task wrapper                                             #
# ------------------------------------------------------------------ #

def request_reset_bg(raw_email: str, ip: str | None = None) -> None:
    """Run the whole request off the response path.

    Also equalises timing: the DB lookup and Resend round-trip happen after
    the response is sent, so a caller can't distinguish a real address from
    an unknown one by how long the request took.
    """
    from database.connection import SessionLocal
    db = SessionLocal()
    try:
        request_reset(db, raw_email, ip=ip)
    except Exception as exc:
        logger.error("Background password reset failed: %s", exc)
    finally:
        db.close()
