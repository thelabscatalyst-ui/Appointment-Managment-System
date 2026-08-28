from passlib.context import CryptContext
from jose import JWTError, jwt
from datetime import datetime, timedelta
from fastapi import Request, Depends, HTTPException, status
from sqlalchemy.orm import Session

from config import settings
from database.connection import get_db

# argon2id is OWASP's first-choice password hash; bcrypt is retained so every
# existing hash still verifies. `deprecated="auto"` marks bcrypt hashes as
# outdated, which lets verify_and_rehash() transparently upgrade a doctor on
# their next successful login — nobody is forced to reset their password.
#
# bcrypt stays pinned at 4.0.1 (passlib 1.7.4 breaks on bcrypt 5.x).
#
# argon2id parameters are pinned explicitly rather than left to passlib's
# defaults (m=64MiB, t=3, p=4). Two reasons:
#   1. Memory. Each concurrent hash allocates memory_cost. At passlib's 64 MiB
#      default, ~8 simultaneous logins would reserve 512 MiB — enough to OOM a
#      small Railway container. 19 MiB keeps that bounded.
#   2. Parallelism. p=4 assumes 4 cores; the container may have fewer, so p=1
#      gives predictable latency instead of contention.
#
# m=19456 KiB / t=3 / p=1 sits at OWASP's recommended minimum for memory with
# time_cost raised one notch to compensate. Measures ~20 ms/verify — still ~8x
# faster than the bcrypt(12) it replaces (~174 ms).
pwd_context = CryptContext(
    schemes=["argon2", "bcrypt"],
    deprecated="auto",
    argon2__type="ID",
    argon2__memory_cost=19456,   # 19 MiB
    argon2__time_cost=3,
    argon2__parallelism=1,
)


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    # Malformed/unknown hashes raise inside passlib; treat as a failed login
    # rather than a 500.
    try:
        return pwd_context.verify(plain, hashed)
    except Exception:
        return False


def verify_and_rehash(plain: str, hashed: str) -> tuple[bool, str | None]:
    """Verify a password and upgrade its hash if the scheme is outdated.

    Returns (is_valid, new_hash). `new_hash` is None when no upgrade is
    needed; when it is a string the caller MUST persist it, e.g.

        ok, new_hash = verify_and_rehash(password, doctor.password_hash)
        if ok and new_hash:
            doctor.password_hash = new_hash
            db.commit()

    This is how legacy bcrypt hashes migrate to argon2id over time.
    """
    try:
        return pwd_context.verify_and_update(plain, hashed)
    except Exception:
        return False, None


# How long a session may live in total, however active the doctor is. Sliding
# renewal keeps an in-use session alive, but not past this — after it, a real
# re-login is required. 12h comfortably covers the longest clinic day.
SESSION_ABSOLUTE_MAX_HOURS = 12


def _utc_timestamp() -> float:
    """Current time as a true UTC epoch.

    Do NOT use `datetime.utcnow().timestamp()` here. utcnow() returns a NAIVE
    datetime, and .timestamp() interprets naive values as LOCAL time — and
    main.py forces TZ=Asia/Kolkata. That shifted `iat` 5.5 hours into the past
    while python-jose wrote `exp` correctly as UTC, inflating the apparent
    token lifetime to 390 minutes. The renewal threshold then sat past the
    token's own expiry, so sliding renewal never fired and doctors were hard
    logged out mid-clinic.
    """
    from datetime import timezone
    return datetime.now(timezone.utc).timestamp()


def create_access_token(data: dict) -> str:
    """Mint a session JWT.

    `iat` and `jti` are set only if the caller hasn't supplied them. That
    matters for sliding renewal: a renewed token must carry the ORIGINAL iat,
    otherwise the absolute cap resets on every renewal and the session could
    live forever.
    """
    import secrets

    payload = data.copy()
    now_ts = _utc_timestamp()
    payload.setdefault("iat", int(now_ts))
    payload.setdefault("jti", secrets.token_urlsafe(8))   # for audit correlation
    payload["exp"] = int(now_ts + settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60)
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def should_renew(payload: dict) -> bool:
    """True when a session should get a fresh expiry.

    Renews once more than half the token's life has elapsed — so an active
    doctor is never logged out mid-consultation, while an abandoned session
    still dies within ACCESS_TOKEN_EXPIRE_MINUTES.

    Returns False past the absolute cap, so renewal cannot extend a session
    indefinitely. Tokens minted before this feature carry no `iat`; those are
    renewed once, which stamps one and starts the cap from that point.
    """
    if not payload:
        return False
    exp = payload.get("exp")
    if not exp:
        return False

    now = _utc_timestamp()
    iat = payload.get("iat")

    if iat:
        if now - float(iat) > SESSION_ABSOLUTE_MAX_HOURS * 3600:
            return False   # past the absolute cap — let it expire
        lifetime = float(exp) - float(iat)
        # Guard against a malformed/instant lifetime.
        if lifetime <= 0:
            return False
        return (now - float(iat)) > (lifetime / 2)

    # Legacy token (no iat): renew so it picks one up, migrating it forward.
    return True


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except JWTError:
        return None


def _token_version_ok(payload: dict, doctor) -> bool:
    """Reject sessions issued before the doctor's last password reset.

    Tokens minted before this feature existed carry no "tv" claim, so they are
    treated as version 0 rather than rejected outright — shipping this does NOT
    log out every doctor currently signed in. Once a reset bumps the doctor to
    version 1, those legacy tokens stop matching and die, which is the intent.
    """
    return int(payload.get("tv", 0)) == int(getattr(doctor, "token_version", 0) or 0)


def get_current_doctor(request: Request, db: Session = Depends(get_db)):
    from database.models import Doctor
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not logged in")
    payload = decode_token(token)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session")
    doctor = db.query(Doctor).filter(Doctor.id == payload.get("doctor_id")).first()
    if not doctor or not doctor.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account not found")
    if not _token_version_ok(payload, doctor):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session ended — your password was changed",
        )
    return doctor


class EmailNotVerified(Exception):
    """Raised when a doctor tries to use the app before confirming their
    email. Verification is mandatory — there is no skip option. Raised
    ahead of the plan check in get_paying_doctor()/get_appt_doctor() so an
    unverified doctor always lands on /verify-email, not a plan-billing
    page that would be confusing before they've even confirmed who they are.
    """
    pass


def _require_verified(doctor) -> None:
    if not doctor.email_verified_at:
        raise EmailNotVerified()


class PlanExpired(Exception):
    """Raised when a doctor's trial and paid plan have both expired.
    reason = 'personal' → show /billing (they can renew themselves)
    reason = 'clinic'   → show /plan-lapsed (they must contact clinic owner)
    """
    def __init__(self, reason: str = "personal"):
        self.reason = reason
        super().__init__(f"Plan expired: {reason}")


class PinRequired(Exception):
    """Raised when a PIN-protected route is hit without a valid PIN session."""
    def __init__(self, return_url: str = "/dashboard"):
        self.return_url = return_url
        super().__init__("PIN required")


PIN_SESSION_MINUTES = 30


def create_pin_token(doctor_id: int) -> str:
    """Create a short-lived JWT for PIN session (30 min)."""
    payload = {
        "doctor_id": doctor_id,
        "pin_ok": True,
        "exp": datetime.utcnow() + timedelta(minutes=PIN_SESSION_MINUTES),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_pin_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        if payload.get("pin_ok"):
            return payload
        return None
    except JWTError:
        return None


def _pin_ok(request: Request, doctor) -> bool:
    """Returns True if PIN session cookie is valid, or if doctor has no PIN set."""
    if not doctor.pin_hash:
        return True
    pin_token = request.cookies.get("pin_session")
    if not pin_token:
        return False
    payload = decode_pin_token(pin_token)
    return bool(payload and payload.get("doctor_id") == doctor.id)


def get_paying_doctor(request: Request, doctor=Depends(get_current_doctor),
                      db: Session = Depends(get_db)):
    """Dependency for all protected routes — plan-gates PER CLINIC.

    Previously this granted access GLOBALLY: it walked every membership and
    unlocked the doctor everywhere if any one clinic qualified. That meant two
    doctors could invite each other and both get a free personal practice
    indefinitely. Access is now scoped to the clinic the request is actually
    operating in:

      * working in a clinic you OWN  -> your own plan, or the clinic's
      * working in someone else's    -> that clinic's plan only

    Verification is still checked first — email confirmation is mandatory to
    use the software at all, not something a lapsed-plan doctor sees instead.

    `request` is taken so the resolved clinic can be stashed on request.state,
    which is how ~59 routes consume it without a single signature change.
    FastAPI injects Request automatically.
    """
    from services.clinic_context import (
        resolve_active_clinic, clinic_plan_active, personal_plan_active, ROLE_OWNER,
    )

    _require_verified(doctor)

    clinic, membership = resolve_active_clinic(request, doctor, db)
    request.state.active_clinic = clinic
    request.state.active_clinic_id = clinic.id if clinic else None
    request.state.active_role = membership.role if membership else None

    if clinic is None:
        # No membership at all — nothing but their own entitlement applies.
        if not personal_plan_active(doctor):
            raise PlanExpired(reason="personal")
        return doctor

    if membership.role == ROLE_OWNER:
        if not (personal_plan_active(doctor) or clinic_plan_active(clinic, db)):
            raise PlanExpired(reason="personal")
    else:
        # Someone else's clinic: only that clinic's entitlement counts, and an
        # associate cannot renew it — hence reason="clinic" (-> /plan-lapsed).
        if not clinic_plan_active(clinic, db):
            raise PlanExpired(reason="clinic")

    return doctor


def _pin_parent_path(path: str) -> str:
    """Map a non-GET path to its parent GET page so redirect lands on the overlay."""
    if path.startswith("/doctors/settings"):
        return "/doctors/settings"
    if path.startswith("/billing"):
        return "/billing"
    if path.startswith("/income"):
        return "/income"
    if path.startswith("/patients/"):
        # e.g. /patients/42/delete → /patients/42
        # e.g. /patients/42/prescriptions is a GET — no mapping needed
        parts = path.split("/")
        if len(parts) >= 3 and parts[2].isdigit():
            return f"/patients/{parts[2]}"
    if path.startswith("/prescriptions/"):
        # e.g. /prescriptions/7/delete → /prescriptions/7
        parts = path.split("/")
        if len(parts) >= 3 and parts[2].isdigit():
            return f"/prescriptions/{parts[2]}"
    return "/dashboard"


def require_pin(request: Request, doctor=Depends(get_paying_doctor)):
    """PIN-protected + plan-gated.
    GET  → sets request.state.pin_required; route renders page with blur overlay.
    POST → raises PinRequired; handler redirects to parent GET (which shows overlay).
    """
    needs = bool(doctor.pin_hash) and not _pin_ok(request, doctor)
    request.state.pin_required = needs
    if needs and request.method != "GET":
        raise PinRequired(return_url=_pin_parent_path(str(request.url.path)))
    return doctor


def require_pin_auth(request: Request, doctor=Depends(get_current_doctor)):
    """PIN-protected billing routes (no plan gate).
    Same GET/POST split as require_pin.
    """
    needs = bool(doctor.pin_hash) and not _pin_ok(request, doctor)
    request.state.pin_required = needs
    if needs and request.method != "GET":
        raise PinRequired(return_url=_pin_parent_path(str(request.url.path)))
    return doctor


def get_appt_doctor(appt_id: int, request: Request, db: Session = Depends(get_db)):
    """
    Dependency for appointment detail / edit / status routes.
    Doctor JWT only — plan-gated. Clinic owners can also access
    their associate doctors' appointments.
    """
    from database.models import Doctor as DoctorModel, Appointment as ApptModel
    from database.models import ClinicDoctor

    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not logged in")
    payload = decode_token(token)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session")

    doctor = db.query(DoctorModel).filter(DoctorModel.id == payload.get("doctor_id")).first()
    if not doctor or not doctor.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account not found")

    # This dependency re-implements token handling instead of calling
    # get_current_doctor, and the version check was missing — so a session
    # minted before a password reset still authenticated on all nine
    # appointment routes, surviving the very reset meant to kill it.
    if not _token_version_ok(payload, doctor):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session ended — your password was changed",
        )

    _require_verified(doctor)

    # Plan gate — shares clinic_context with get_paying_doctor rather than
    # duplicating the ladder. The copy this replaces silently omitted the
    # plan_grace_until check, so a clinic inside its grace window was treated
    # as lapsed on these nine routes but active everywhere else.
    from services.clinic_context import (
        resolve_active_clinic, clinic_plan_active, personal_plan_active,
        is_owner_of, ROLE_OWNER,
    )

    clinic, membership = resolve_active_clinic(request, doctor, db)
    request.state.active_clinic = clinic
    request.state.active_clinic_id = clinic.id if clinic else None
    request.state.active_role = membership.role if membership else None

    if clinic is None:
        if not personal_plan_active(doctor):
            raise PlanExpired(reason="personal")
    elif membership.role == ROLE_OWNER:
        if not (personal_plan_active(doctor) or clinic_plan_active(clinic, db)):
            raise PlanExpired(reason="personal")
    else:
        if not clinic_plan_active(clinic, db):
            raise PlanExpired(reason="clinic")

    # A clinic owner may act on their doctors' appointments — but only for
    # appointments that happened AT the clinic they are currently in. The old
    # check took an arbitrary owned membership with no is_active filter and
    # never looked at where the appointment happened, so an owner of clinic A
    # could reach an associate's appointment booked at clinic B.
    appt_row = db.query(ApptModel).filter(ApptModel.id == appt_id).first()
    if appt_row and appt_row.doctor_id != doctor.id and clinic is not None:
        same_clinic = (
            appt_row.clinic_id == clinic.id
            or appt_row.clinic_id is None  # legacy rows the backfill couldn't place
        )
        if same_clinic and is_owner_of(db, doctor.id, clinic.id):
            member = db.query(ClinicDoctor).filter(
                ClinicDoctor.clinic_id == clinic.id,
                ClinicDoctor.doctor_id == appt_row.doctor_id,
                ClinicDoctor.is_active == True,
            ).first()
            if member:
                actual_doctor = db.query(DoctorModel).filter(
                    DoctorModel.id == appt_row.doctor_id
                ).first()
                if actual_doctor:
                    # Audit trail: record who actually acted, not who they
                    # acted as. created_by columns read real_doctor_id.
                    request.state.real_doctor_id = doctor.id
                    request.state.acting_as_doctor_id = actual_doctor.id
                    return actual_doctor

    return doctor


def get_admin_doctor(doctor=Depends(get_current_doctor)):
    """Dependency for /admin routes — only allows the platform owner."""
    from config import settings
    if not settings.ADMIN_EMAIL or doctor.email.lower() != settings.ADMIN_EMAIL.lower():
        raise HTTPException(status_code=403, detail="Admin access required")
    return doctor


def get_clinic_owner(request: Request, db: Session = Depends(get_db)):
    """Dependency for /clinic/admin routes — doctor who owns a REAL clinic plan (not solo trial).

    Delegates to clinic_context.owned_clinic, which (unlike the query this
    replaced) also filters on is_active and compares plan_expires_at to now.
    The old version accepted a deactivated owner membership, and let a clinic
    that had stopped paying keep Clinic Admin indefinitely.
    """
    from services.clinic_context import owned_clinic
    doctor = get_current_doctor(request, db)
    clinic = owned_clinic(db, doctor.id, require_paid=True)
    if not clinic:
        raise HTTPException(status_code=403, detail="Clinic plan required")
    request.state.clinic = clinic
    return doctor
