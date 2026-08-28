"""
services/clinic_context.py — clinic membership, roles, and the active clinic.

Before this module there were sixteen hand-rolled "is this doctor an owner?"
queries spread across main.py, auth_service, and four routers, using four
different filter sets: some checked is_active, some didn't; some checked
Clinic.plan_type, some didn't; three checked no role at all and just took the
first membership row. Which clinic a dual-role doctor "belonged to" therefore
depended on insertion order and on which code path happened to ask.

Everything role- or clinic-related now resolves through here.

Roles are module constants rather than a SQLAlchemy enum on purpose.
ClinicDoctor.role is a plain String(20); converting it would make create_all
emit CREATE TYPE on PostgreSQL, which then needs hand-written ALTER TYPE to
evolve and cannot be altered at all on SQLite. database/connection.py already
carries one such scar (the visitstatus ALTER TYPE guarded by `if not
_is_sqlite`). Constants also keep the raw-SQL migrations — which compare
`role = 'owner'` directly — working unchanged.
"""
from datetime import datetime

from sqlalchemy.orm import Session

ROLE_OWNER = "owner"
ROLE_ASSOCIATE = "associate"

# Cookie carrying the doctor's chosen clinic. Signed with the app SECRET_KEY,
# same pattern as pin_session and clinic_admin_auth.
ACTIVE_CLINIC_COOKIE = "active_clinic"
ACTIVE_CLINIC_MAX_AGE = 30 * 24 * 3600   # 30 days — a preference, not a credential


# --------------------------------------------------------------------------- #
#  Membership                                                                   #
# --------------------------------------------------------------------------- #

def active_memberships(db: Session, doctor_id: int) -> list:
    """Every live membership for a doctor, owned clinics first.

    Deterministic ordering (owner before associate, then clinic_id) so that
    "the doctor's clinic" never depends on insertion order, which is what the
    three old _get_primary_clinic helpers did.
    """
    from database.models import ClinicDoctor
    rows = (
        db.query(ClinicDoctor)
        .filter(
            ClinicDoctor.doctor_id == doctor_id,
            ClinicDoctor.is_active == True,   # noqa: E712 — SQL, not Python
        )
        .all()
    )
    return sorted(rows, key=lambda m: (0 if m.role == ROLE_OWNER else 1, m.clinic_id))


def get_membership(db: Session, doctor_id: int, clinic_id: int):
    """The doctor's live membership in one specific clinic, or None."""
    from database.models import ClinicDoctor
    if not clinic_id:
        return None
    return (
        db.query(ClinicDoctor)
        .filter(
            ClinicDoctor.doctor_id == doctor_id,
            ClinicDoctor.clinic_id == clinic_id,
            ClinicDoctor.is_active == True,   # noqa: E712
        )
        .first()
    )


def is_owner_of(db: Session, doctor_id: int, clinic_id: int) -> bool:
    m = get_membership(db, doctor_id, clinic_id)
    return bool(m and m.role == ROLE_OWNER)


# --------------------------------------------------------------------------- #
#  Plan state                                                                   #
# --------------------------------------------------------------------------- #

def clinic_plan_active(clinic, db: Session, now: datetime | None = None) -> bool:
    """Is this clinic currently entitled to service?

    This is the fix for the biggest gap in the old code: NOT ONE of the ten
    ownership checks compared Clinic.plan_expires_at to now. They all tested
    `plan_type == "clinic"`, which billing_verify sets and nothing ever unsets
    on expiry — so a clinic that stopped paying kept Clinic Admin forever.

    The ladder below is lifted from the loop that used to live inside
    get_paying_doctor, which was the only place plan_grace_until was honoured.
    """
    from database.models import Doctor
    if clinic is None:
        return False
    now = now or datetime.utcnow()

    if clinic.plan_expires_at and clinic.plan_expires_at > now:
        return True

    grace = getattr(clinic, "plan_grace_until", None)
    if grace and grace > now:
        return True

    # A clinic on its owner's trial has no plan_expires_at of its own.
    if clinic.owner_doctor_id:
        owner = db.query(Doctor).filter(Doctor.id == clinic.owner_doctor_id).first()
        if owner and (
            (owner.trial_ends_at and owner.trial_ends_at > now)
            or (owner.plan_expires_at and owner.plan_expires_at > now)
        ):
            return True

    return False


def personal_plan_active(doctor, now: datetime | None = None) -> bool:
    """The doctor's own trial or paid plan — nothing to do with any clinic."""
    now = now or datetime.utcnow()
    return bool(
        (doctor.trial_ends_at and doctor.trial_ends_at > now)
        or (doctor.plan_expires_at and doctor.plan_expires_at > now)
    )


def owned_clinic(db: Session, doctor_id: int, *, require_paid: bool = False,
                 now: datetime | None = None):
    """The clinic this doctor owns, or None.

    require_paid=True additionally demands a real multi-doctor entitlement —
    plan_type "clinic" AND a live plan. That pairing is what gates Clinic
    Admin: plan_type alone let lapsed clinics in forever.
    """
    from database.models import Clinic
    for m in active_memberships(db, doctor_id):
        if m.role != ROLE_OWNER:
            continue
        clinic = db.query(Clinic).filter(Clinic.id == m.clinic_id).first()
        if not clinic:
            continue
        if not require_paid:
            return clinic
        if clinic.plan_type == "clinic" and clinic_plan_active(clinic, db, now):
            return clinic
    return None


def is_clinic_owner(db: Session, doctor_id: int, now: datetime | None = None) -> bool:
    """Does this doctor own a clinic with a live multi-doctor entitlement?"""
    return owned_clinic(db, doctor_id, require_paid=True, now=now) is not None


# --------------------------------------------------------------------------- #
#  Active clinic                                                                #
# --------------------------------------------------------------------------- #

def _decode_active_clinic_cookie(request, doctor_id: int) -> int | None:
    """Read the clinic id out of the cookie, or None.

    Bound to doctor_id so a cookie left behind by another account on a shared
    terminal is inert rather than silently switching context.
    """
    from services.auth_service import decode_token
    raw = request.cookies.get(ACTIVE_CLINIC_COOKIE)
    if not raw:
        return None
    payload = decode_token(raw)
    if not payload or payload.get("doctor_id") != doctor_id:
        return None
    return payload.get("clinic_id")


def resolve_active_clinic(request, doctor, db: Session):
    """Which clinic is this doctor working in right now?

    Returns (clinic, membership) — both None when the doctor has no live
    membership at all, which must degrade rather than 500.

    The cookie is only ever a hint. Membership is re-checked against the
    database on every request, because a doctor removed from a clinic still
    holds a validly-signed cookie naming it.
    """
    from database.models import Clinic

    memberships = active_memberships(db, doctor.id)
    if not memberships:
        return None, None

    valid_ids = {m.clinic_id: m for m in memberships}

    # 1. Explicit override, for routes that opt into ?clinic_id= — still
    #    validated against real membership.
    raw_qs = request.query_params.get("clinic_id") if hasattr(request, "query_params") else None
    if raw_qs:
        try:
            if int(raw_qs) in valid_ids:
                m = valid_ids[int(raw_qs)]
                return db.query(Clinic).filter(Clinic.id == m.clinic_id).first(), m
        except (TypeError, ValueError):
            pass

    # 2. The cookie, if it still names a clinic they belong to.
    cookie_id = _decode_active_clinic_cookie(request, doctor.id)
    if cookie_id in valid_ids:
        m = valid_ids[cookie_id]
        return db.query(Clinic).filter(Clinic.id == m.clinic_id).first(), m

    # 3. Default: owned clinic first (active_memberships already sorts that way).
    m = memberships[0]
    return db.query(Clinic).filter(Clinic.id == m.clinic_id).first(), m


def set_active_clinic_cookie(response, doctor_id: int, clinic_id: int) -> None:
    from jose import jwt
    from config import settings
    import time

    token = jwt.encode(
        {
            "doctor_id": doctor_id,
            "clinic_id": clinic_id,
            "exp": int(time.time()) + ACTIVE_CLINIC_MAX_AGE,
        },
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )
    response.set_cookie(
        ACTIVE_CLINIC_COOKIE,
        token,
        httponly=True,
        secure=settings.ENVIRONMENT.lower() == "production",
        samesite="lax",
        max_age=ACTIVE_CLINIC_MAX_AGE,
    )
