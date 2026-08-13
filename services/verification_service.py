"""
verification_service.py — email ownership verification via one-time codes.

Design follows OWASP ASVS §2.7 and NIST SP 800-63B §5.1.4:

  * 6-digit codes from `secrets` (CSPRNG), never `random`.
  * Stored **hashed**. DB read access must not yield live codes.
  * 10-minute expiry — NIST caps out-of-band validity at 10 minutes.
  * 5 attempts, then the code is burned. A 10^6 space is trivially
    brute-forced otherwise.
  * Single use, enforced by `consumed_at`.
  * Issuing a new code invalidates all prior unconsumed ones, so exactly one
    code is ever live per doctor.

Verification is deliberately NOT a login gate — see `routers/auth.py`. An
unverified doctor is alone in their own tenant, so blocking login would be
disproportionate and would strand them if mail delivery hiccups.
"""
import logging
import secrets
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from database.models import Doctor, EmailVerification
from services.auth_service import hash_password, verify_password
from services.email_service import send_email, render_email, code_block

logger = logging.getLogger(__name__)

CODE_LENGTH        = 6
CODE_TTL_MINUTES   = 10    # NIST SP 800-63B §5.1.4.2
MAX_ATTEMPTS       = 5
RESEND_COOLDOWN_S  = 60
MAX_CODES_PER_HOUR = 5


def _generate_code() -> str:
    """A zero-padded 6-digit code from a cryptographically secure source."""
    return f"{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}"


def _active_code(db: Session, doctor_id: int) -> EmailVerification | None:
    """The most recent unconsumed, unexpired code for this doctor."""
    return (
        db.query(EmailVerification)
        .filter(
            EmailVerification.doctor_id == doctor_id,
            EmailVerification.consumed_at.is_(None),
            EmailVerification.expires_at > datetime.utcnow(),
        )
        .order_by(EmailVerification.id.desc())
        .first()
    )


def codes_issued_last_hour(db: Session, doctor_id: int) -> int:
    return (
        db.query(EmailVerification)
        .filter(
            EmailVerification.doctor_id == doctor_id,
            EmailVerification.created_at > datetime.utcnow() - timedelta(hours=1),
        )
        .count()
    )


def seconds_until_resend(db: Session, doctor_id: int) -> int:
    """Remaining cooldown before another code may be requested (0 = ready)."""
    latest = (
        db.query(EmailVerification)
        .filter(EmailVerification.doctor_id == doctor_id)
        .order_by(EmailVerification.id.desc())
        .first()
    )
    if not latest or not latest.created_at:
        return 0
    elapsed = (datetime.utcnow() - latest.created_at).total_seconds()
    return max(0, int(RESEND_COOLDOWN_S - elapsed))


def issue_code(
    db: Session, doctor: Doctor, *, bypass_cooldown: bool = False
) -> tuple[bool, str]:
    """Create and send a verification code.

    Returns (ok, detail). `detail` is a user-safe reason on failure.
    The code itself is returned to the caller ONLY via the email.

    `bypass_cooldown` skips the 60s resend throttle. Used when the doctor
    corrects a mistyped address: that's a deliberate action targeting a NEW
    mailbox, so throttling it would silently strand them on the very screen
    meant to rescue them. The hourly cap and route rate limiting still apply.
    """
    if doctor.email_verified_at:
        return False, "already verified"

    if not bypass_cooldown:
        wait = seconds_until_resend(db, doctor.id)
        if wait > 0:
            return False, f"Please wait {wait}s before requesting another code."

    if codes_issued_last_hour(db, doctor.id) >= MAX_CODES_PER_HOUR:
        return False, "Too many codes requested. Try again in an hour."

    # Only one live code at a time — burn any previous unconsumed ones so an
    # older email can't still be used.
    db.query(EmailVerification).filter(
        EmailVerification.doctor_id == doctor.id,
        EmailVerification.consumed_at.is_(None),
    ).update({"consumed_at": datetime.utcnow()}, synchronize_session=False)

    code = _generate_code()
    record = EmailVerification(
        doctor_id  = doctor.id,
        email      = doctor.email,
        code_hash  = hash_password(code),
        expires_at = datetime.utcnow() + timedelta(minutes=CODE_TTL_MINUTES),
        attempts   = 0,
    )
    db.add(record)
    db.commit()

    _send_code_email(doctor.email, doctor.name, code)
    return True, "sent"


def _send_code_email(to_email: str, name: str, code: str) -> None:
    first_name = (name or "there").strip().split(" ")[0]
    body = f"""
      <h2 style="margin:0 0 12px;font-size:20px;font-weight:700;color:#1a1410;">
        Confirm your email
      </h2>
      <p style="margin:0 0 4px;color:#5c5148;">
        Hi {first_name}, enter this code in Nivora to verify your email address:
      </p>
      {code_block(code)}
      <p style="margin:0;color:#5c5148;">
        This code expires in {CODE_TTL_MINUTES} minutes and can only be used once.
      </p>
      <p style="margin:14px 0 0;font-size:13px;color:#8a7f74;">
        If you didn't create a Nivora account, you can ignore this email —
        no account will be activated without this code.
      </p>
    """
    send_email(
        to=to_email,
        subject=f"{code} is your Nivora verification code",
        html=render_email(body),
    )


def verify_code(db: Session, doctor: Doctor, submitted: str) -> tuple[bool, str]:
    """Check a submitted code and mark the doctor verified on success.

    Returns (ok, message). Failure messages are safe to show the user.
    """
    if doctor.email_verified_at:
        return True, "Already verified."

    submitted = (submitted or "").strip().replace(" ", "").replace("-", "")
    if not submitted.isdigit() or len(submitted) != CODE_LENGTH:
        return False, f"Enter the {CODE_LENGTH}-digit code from your email."

    record = _active_code(db, doctor.id)
    if not record:
        return False, "That code has expired. Request a new one."

    if record.attempts >= MAX_ATTEMPTS:
        record.consumed_at = datetime.utcnow()   # burn it
        db.commit()
        return False, "Too many incorrect attempts. Request a new code."

    # Count the attempt BEFORE checking, so a crash mid-verify can't be used
    # to retry for free.
    record.attempts += 1
    db.commit()

    if not verify_password(submitted, record.code_hash):
        remaining = MAX_ATTEMPTS - record.attempts
        if remaining <= 0:
            record.consumed_at = datetime.utcnow()
            db.commit()
            return False, "Too many incorrect attempts. Request a new code."
        return False, f"Incorrect code. {remaining} attempt{'s' if remaining != 1 else ''} left."

    # A code is only valid for the address it was sent to — guards the
    # change-address path from verifying a different mailbox.
    if record.email != doctor.email:
        record.consumed_at = datetime.utcnow()
        db.commit()
        return False, "Your email changed since this code was sent. Request a new one."

    record.consumed_at      = datetime.utcnow()
    doctor.email_verified_at = datetime.utcnow()
    db.commit()
    logger.info("Email verified for doctor #%s (%s)", doctor.id, doctor.email)
    return True, "Email verified."


def change_email(db: Session, doctor: Doctor, new_email: str) -> tuple[bool, str]:
    """Correct a mistyped address while still unverified, then re-send.

    A typo at signup is the most common reason a code never arrives, so this
    is the primary self-service fallback.
    """
    from routers.auth import _normalise_email

    if doctor.email_verified_at:
        return False, "Your email is already verified."

    normalised = _normalise_email(new_email)
    if not normalised or "@" not in normalised or "." not in normalised.split("@")[-1]:
        return False, "Enter a valid email address."

    if normalised == doctor.email:
        return False, "That's already your email address."

    taken = db.query(Doctor).filter(
        Doctor.email == normalised, Doctor.id != doctor.id
    ).first()
    if taken:
        # Generic — do not reveal that another account holds this address.
        return False, "That address can't be used. Try a different one."

    doctor.email = normalised
    # Any code issued to the old address is now void.
    db.query(EmailVerification).filter(
        EmailVerification.doctor_id == doctor.id,
        EmailVerification.consumed_at.is_(None),
    ).update({"consumed_at": datetime.utcnow()}, synchronize_session=False)
    db.commit()

    # Bypass the resend throttle: this targets a different mailbox, and the
    # doctor is here precisely because the last code never arrived.
    ok, detail = issue_code(db, doctor, bypass_cooldown=True)
    if not ok:
        return True, f"Email updated to {normalised}, but the code could not be sent: {detail}"
    return True, f"Email updated. We've sent a new code to {normalised}."


# ------------------------------------------------------------------ #
#  Background-task wrapper                                             #
# ------------------------------------------------------------------ #

def issue_code_bg(doctor_id: int) -> None:
    """Issue a code after the response is sent.

    Opens its own session and re-queries by ID — the request-scoped session
    is closed by the time this runs (same rule as notification_service).
    """
    from database.connection import SessionLocal
    db = SessionLocal()
    try:
        doctor = db.query(Doctor).filter(Doctor.id == doctor_id).first()
        if doctor:
            issue_code(db, doctor)
    except Exception as exc:
        logger.error("Background code issue failed for doctor #%s: %s", doctor_id, exc)
    finally:
        db.close()
