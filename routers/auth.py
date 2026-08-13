import re
from datetime import datetime, timedelta
from fastapi import APIRouter, Request, Depends, Form, Query, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database.connection import get_db
from database.models import Doctor, PlanType, Clinic, ClinicDoctor, ClinicDoctorInvite
from config import settings
from services.auth_service import (
    hash_password, verify_password, verify_and_rehash,
    create_access_token, decode_token, get_current_doctor,
)
from services.password_policy import validate_password

router = APIRouter(tags=["auth"])
templates = Jinja2Templates(directory="templates")

# Generic message for duplicate email OR phone. Deliberately does not reveal
# which field collided, or whether the account exists at all — otherwise
# /register becomes a probe for "is this doctor on Nivora?".
_DUPLICATE_MSG = (
    "That email or phone number is already registered. "
    "Try logging in, or reset your password."
)


def _normalise_email(email: str) -> str:
    """Canonical form used for BOTH duplicate checks and storage.

    Previously the duplicate check compared the raw input while the insert
    stored a lowercased value, so 'Foo@x.com' passed the check and then hit
    the DB unique constraint as a 500.
    """
    return (email or "").strip().lower()


def _normalise_phone(phone: str) -> str:
    """Canonical phone form, reusing the existing E.164 helper."""
    from services.notification_service import _e164
    cleaned = (phone or "").strip()
    if not cleaned:
        return ""
    try:
        return _e164(cleaned)
    except Exception:
        return cleaned


def _make_slug(name: str, city: str) -> str:
    """Generate a URL-safe slug from doctor name + city."""
    raw = f"{name}-{city}".lower()
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return slug


def _unique_slug(base: str, db: Session) -> str:
    slug = base
    counter = 1
    while db.query(Doctor).filter(Doctor.slug == slug).first():
        slug = f"{base}-{counter}"
        counter += 1
    return slug


# ------------------------------------------------------------------ #
#  Register                                                            #
# ------------------------------------------------------------------ #

@router.get("/register", response_class=HTMLResponse)
def register_page(
    request: Request,
    clinic_invite: str = Query(default=""),
    plan: str = Query(default=""),
    db: Session = Depends(get_db),
):
    # Redirect already-logged-in users away from register
    token = request.cookies.get("access_token")
    if token and decode_token(token):
        return RedirectResponse(url="/dashboard", status_code=303)

    joining_clinic = None
    if clinic_invite:
        invite = db.query(ClinicDoctorInvite).filter(
            ClinicDoctorInvite.token == clinic_invite,
            ClinicDoctorInvite.used_at == None,
            ClinicDoctorInvite.expires_at > datetime.utcnow(),
        ).first()
        if invite:
            joining_clinic = db.query(Clinic).filter(Clinic.id == invite.clinic_id).first()

    plan_hint = plan if plan in ("solo", "clinic") else "solo"
    return templates.TemplateResponse(request, "register.html", {
        "error": None,
        "clinic_invite": clinic_invite,
        "joining_clinic": joining_clinic,
        "plan_hint": plan_hint,
    })


@router.post("/register", response_class=HTMLResponse)
def register(
    request: Request,
    background_tasks: BackgroundTasks,
    name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(...),
    password: str = Form(...),
    specialization: str = Form(""),
    clinic_name: str = Form(""),
    city: str = Form(""),
    clinic_invite: str = Form(""),
    medical_reg_number: str = Form(""),
    db: Session = Depends(get_db),
):
    invite_token = clinic_invite.strip()

    # Normalise BEFORE any lookup so the duplicate check and the eventual
    # insert compare the exact same value.
    norm_email = _normalise_email(email)
    norm_phone = _normalise_phone(phone)

    def _reject(message: str, status_code: int = 400):
        return templates.TemplateResponse(
            request, "register.html",
            {"error": message, "clinic_invite": invite_token,
             "joining_clinic": None, "plan_hint": "solo"},
            status_code=status_code,
        )

    # ── Basic field validation ────────────────────────────────────────────
    if not norm_email or "@" not in norm_email or "." not in norm_email.split("@")[-1]:
        return _reject("Enter a valid email address.")

    digits = re.sub(r"\D", "", norm_phone)
    if len(digits) < 10:
        return _reject("Enter a valid 10-digit phone number.")

    # ── Password policy (server-side; the HTML minlength is not a control) ─
    problems = validate_password(
        password, email=norm_email, name=name, clinic_name=clinic_name,
    )
    if problems:
        return _reject(" ".join(problems))

    # ── Duplicate check — one generic message for both fields ─────────────
    # Phone is matched against BOTH the normalised (+91…) and raw-stripped
    # forms: rows created before normalisation store the raw value, and
    # Doctor.phone is UNIQUE — missing a legacy match would turn a friendly
    # 400 into an IntegrityError 500.
    phone_candidates = {norm_phone, phone.strip()}
    phone_candidates.discard("")
    existing = db.query(Doctor).filter(
        (Doctor.email == norm_email) | (Doctor.phone.in_(phone_candidates))
    ).first()
    if existing:
        return _reject(_DUPLICATE_MSG)

    slug = _unique_slug(_make_slug(name, city or "clinic"), db)

    # Check for valid clinic invite BEFORE creating the doctor
    valid_invite = None
    if invite_token:
        valid_invite = db.query(ClinicDoctorInvite).filter(
            ClinicDoctorInvite.token == invite_token,
            ClinicDoctorInvite.used_at == None,
            ClinicDoctorInvite.expires_at > datetime.utcnow(),
        ).first()

    if valid_invite:
        # ── Clinic member path: no trial, no solo clinic ──────────────────────
        doctor = Doctor(
            name=name,
            email=norm_email,
            phone=norm_phone,
            password_hash=hash_password(password),
            specialization=specialization.strip() or None,
            clinic_name=None,    # will show joined clinic name from Clinic table
            city=city.strip() or None,
            slug=slug,
            plan_type=PlanType.trial,
            trial_ends_at=None,  # no trial — access gated by clinic plan
            plan_expires_at=None,
            medical_reg_number=medical_reg_number.strip() or None,
        )
        db.add(doctor)
        db.commit()
        db.refresh(doctor)

        db.add(ClinicDoctor(
            clinic_id=valid_invite.clinic_id,
            doctor_id=doctor.id,
            role="associate",
            is_active=True,
        ))
        valid_invite.used_at = datetime.utcnow()
        db.commit()

    else:
        # ── Solo doctor path: 14-day trial + auto solo clinic ─────────────────
        doctor = Doctor(
            name=name,
            email=norm_email,
            phone=norm_phone,
            password_hash=hash_password(password),
            specialization=specialization.strip() or None,
            clinic_name=clinic_name.strip() or None,
            city=city.strip() or None,
            slug=slug,
            plan_type=PlanType.trial,
            trial_ends_at=datetime.utcnow() + timedelta(days=14),
            medical_reg_number=medical_reg_number.strip() or None,
        )
        db.add(doctor)
        db.commit()
        db.refresh(doctor)

        # Auto-create an implicit clinic for every solo doctor (owner role)
        clinic_slug = slug + "-clinic"
        base_clinic_slug = clinic_slug
        counter = 1
        while db.query(Clinic).filter(Clinic.slug == clinic_slug).first():
            clinic_slug = f"{base_clinic_slug}-{counter}"
            counter += 1

        clinic = Clinic(
            name=clinic_name.strip() or f"{name}'s Clinic",
            address=None,
            city=city.strip() or None,
            slug=clinic_slug,
            plan_type="trial",
            owner_doctor_id=doctor.id,
        )
        db.add(clinic)
        db.commit()
        db.refresh(clinic)

        db.add(ClinicDoctor(
            clinic_id=clinic.id,
            doctor_id=doctor.id,
            role="owner",
            is_active=True,
        ))
        db.commit()

    # Send the verification code after the response — the Resend round-trip
    # must not delay the redirect. Passes the ID only; the wrapper opens its
    # own session (the request-scoped one is closed by then).
    from services.verification_service import issue_code_bg
    background_tasks.add_task(issue_code_bg, doctor.id)

    # Log the doctor straight in and land them on the verification screen.
    # Previously this redirected to /login?registered=1, so a new signup was
    # never told a code had been sent — the verification prompt only appeared
    # after they separately logged in, which made the whole step invisible.
    token = create_access_token({"doctor_id": doctor.id})
    response = RedirectResponse(url="/verify-email", status_code=303)
    response.set_cookie(
        key="access_token", value=token,
        httponly=True, secure=settings.ENVIRONMENT.lower() == "production",
        max_age=60 * 60 * 24, samesite="lax",
    )
    return response


# ------------------------------------------------------------------ #
#  Login                                                               #
# ------------------------------------------------------------------ #

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, registered: str = "", next: str = ""):
    # Redirect already-logged-in users away from login
    token = request.cookies.get("access_token")
    if token and decode_token(token):
        return RedirectResponse(url="/dashboard", status_code=303)
    success = "Account created! Please log in." if registered == "1" else None
    return templates.TemplateResponse(request, "login.html", {
        "error": None, "success": success, "next": next,
    })


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form(default=""),
    db: Session = Depends(get_db),
):
    normalized_email = _normalise_email(email)

    # ── Try doctor first ──────────────────────────────────────────────────────
    doctor = db.query(Doctor).filter(Doctor.email == normalized_email).first()
    if doctor:
        # verify_and_rehash also upgrades legacy bcrypt hashes to argon2id
        # transparently on a successful login.
        password_ok, upgraded_hash = verify_and_rehash(password, doctor.password_hash)
        if not password_ok:
            return templates.TemplateResponse(
                request, "login.html",
                {"error": "Invalid email or password.", "success": None, "next": next},
                status_code=401,
            )
        if upgraded_hash:
            doctor.password_hash = upgraded_hash
            db.commit()
        if not doctor.is_active:
            return templates.TemplateResponse(
                request, "login.html",
                {"error": "Your account has been deactivated.", "success": None, "next": next},
                status_code=403,
            )
        token = create_access_token({"doctor_id": doctor.id})
        # Honor the `next` param — only relative paths, no open redirect
        safe_next = next.strip() if (
            next and next.startswith("/") and not next.startswith("//")
            and not next.startswith("/login") and not next.startswith("/register")
        ) else ""
        redirect_url = safe_next if safe_next else "/workspace-loading"
        response = RedirectResponse(url=redirect_url, status_code=303)
        response.set_cookie(
            key="access_token", value=token,
            httponly=True, secure=settings.ENVIRONMENT.lower() == "production", max_age=60 * 60 * 24, samesite="lax",
        )
        return response

    return templates.TemplateResponse(
        request, "login.html",
        {"error": "Invalid email or password.", "success": None, "next": next},
        status_code=401,
    )


# ------------------------------------------------------------------ #
#  Logout                                                              #
# ------------------------------------------------------------------ #

@router.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    # Clear EVERY auth cookie. Previously only access_token was deleted, so a
    # logged-out session left a live pin_session / clinic_admin_auth behind —
    # logging back in silently skipped the PIN gate.
    for cookie in ("access_token", "pin_session", "clinic_admin_auth"):
        response.delete_cookie(cookie)
    return response


# ------------------------------------------------------------------ #
#  Email verification                                                  #
# ------------------------------------------------------------------ #
# Verification is NOT a login gate. An unverified doctor is alone in their
# own tenant, so blocking login would be disproportionate — and if mail
# delivery hiccups it would lock them out of software they may have paid
# for. Instead they log in normally, see a persistent banner, and certain
# actions (notably password reset) require a verified address.

@router.get("/verify-email", response_class=HTMLResponse)
def verify_email_page(
    request: Request,
    doctor: Doctor = Depends(get_current_doctor),
    db: Session = Depends(get_db),
):
    from services.verification_service import seconds_until_resend

    if doctor.email_verified_at:
        return RedirectResponse(url="/dashboard?verified=1", status_code=303)

    return templates.TemplateResponse(request, "verify_email.html", {
        "doctor":         doctor,
        "error":          None,
        "success":        None,
        "resend_wait":    seconds_until_resend(db, doctor.id),
        "show_change":    request.query_params.get("change") == "1",
    })


@router.post("/verify-email", response_class=HTMLResponse)
def verify_email_submit(
    request: Request,
    code: str = Form(...),
    doctor: Doctor = Depends(get_current_doctor),
    db: Session = Depends(get_db),
):
    from services.verification_service import verify_code, seconds_until_resend

    ok, message = verify_code(db, doctor, code)
    if ok:
        return RedirectResponse(url="/dashboard?verified=1", status_code=303)

    return templates.TemplateResponse(request, "verify_email.html", {
        "doctor":      doctor,
        "error":       message,
        "success":     None,
        "resend_wait": seconds_until_resend(db, doctor.id),
        "show_change": False,
    }, status_code=400)


@router.post("/verify-email/resend", response_class=HTMLResponse)
def verify_email_resend(
    request: Request,
    doctor: Doctor = Depends(get_current_doctor),
    db: Session = Depends(get_db),
):
    from services.verification_service import issue_code, seconds_until_resend

    ok, detail = issue_code(db, doctor)
    return templates.TemplateResponse(request, "verify_email.html", {
        "doctor":      doctor,
        "error":       None if ok else detail,
        "success":     f"We've sent a new code to {doctor.email}." if ok else None,
        "resend_wait": seconds_until_resend(db, doctor.id),
        "show_change": False,
    })


@router.post("/verify-email/change-address", response_class=HTMLResponse)
def verify_email_change_address(
    request: Request,
    new_email: str = Form(...),
    doctor: Doctor = Depends(get_current_doctor),
    db: Session = Depends(get_db),
):
    """Correct a mistyped address. A typo at signup is the most common
    reason a verification code never arrives."""
    from services.verification_service import change_email, seconds_until_resend

    ok, message = change_email(db, doctor, new_email)
    return templates.TemplateResponse(request, "verify_email.html", {
        "doctor":      doctor,
        "error":       None if ok else message,
        "success":     message if ok else None,
        "resend_wait": seconds_until_resend(db, doctor.id),
        "show_change": not ok,
    }, status_code=200 if ok else 400)
