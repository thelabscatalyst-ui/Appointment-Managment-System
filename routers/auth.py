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
# /register becomes a probe for "is this doctor on Med Track?".
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

def _safe_next(request: Request, raw: str) -> str:
    """A `next=` target we are willing to redirect a browser to with GET.

    Two separate rules:

      * site-relative only, and never back to /login or /register — the
        open-redirect and loop guards this has always had;
      * the path must actually answer GET. A `next` naming a POST-only route
        (the switcher's /clinic/switch, captured when a session expired
        mid-page) produced a 405 the moment login succeeded, and since the
        login form carries `next` in a hidden field, every retry hit it again.

    Route matching is asked of the live app rather than a hardcoded list, so a
    POST-only route added later is covered without anyone remembering to.
    """
    from starlette.routing import Match

    value = (raw or "").strip()
    if not (value.startswith("/") and not value.startswith("//")):
        return ""
    if value.startswith("/login") or value.startswith("/register"):
        return ""

    scope = {
        "type": "http",
        "method": "GET",
        "path": value.split("?", 1)[0],
        "root_path": "",
        "headers": [],
        "query_string": b"",
    }
    for route in request.app.routes:
        try:
            # Match.FULL means path AND method match; a POST-only route on the
            # same path matches only PARTIAL, which is exactly the 405 case.
            if route.matches(scope)[0] == Match.FULL:
                return value
        except Exception:
            continue
    return ""


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
    invite_email = None
    invite_error = None
    if clinic_invite:
        invite = db.query(ClinicDoctorInvite).filter(
            ClinicDoctorInvite.token == clinic_invite,
            ClinicDoctorInvite.used_at == None,
            ClinicDoctorInvite.expires_at > datetime.utcnow(),
        ).first()
        if invite:
            joining_clinic = db.query(Clinic).filter(Clinic.id == invite.clinic_id).first()
            # The invite is bound to one address and a mismatch is rejected on
            # submit, so pre-fill it. Leaving it blank invited people to type a
            # different address, fill the whole form, and only then be told —
            # which reads as "the invite link doesn't work".
            invite_email = invite.email
        else:
            # Say so NOW, not after they have filled in eight fields. The token
            # still travels in the hidden field, so the POST refuses too; this
            # just stops them wasting the effort.
            invite_error = (
                "This invite link is no longer valid — it may have expired or "
                "already been used. Ask the clinic to send you a new one. You "
                "can still create your own account below."
            )

    plan_hint = plan if plan in ("solo", "clinic") else "solo"
    return templates.TemplateResponse(request, "register.html", {
        "error": invite_error,
        "clinic_invite": clinic_invite if joining_clinic else "",
        "joining_clinic": joining_clinic,
        "invite_email": invite_email,
        "plan_hint": plan_hint,
    })


def _create_owned_clinic(db: Session, doctor, slug: str, name: str,
                         clinic_name: str, city: str,
                         is_clinic_signup: bool, trial_ends_at):
    """Create the doctor's own Clinic plus their owner membership.

    Shared by the solo path and the invited-doctor-who-also-practises path so
    the slug-uniquing and seat logic exist once.

    A "Clinic Account" signup gets Clinic Admin for the trial window:
    plan_expires_at mirrors the doctor's own trial_ends_at, so it lapses at
    the same moment a solo trial would.
    """
    clinic_slug = slug + "-clinic"
    base_clinic_slug = clinic_slug
    counter = 1
    while db.query(Clinic).filter(Clinic.slug == clinic_slug).first():
        clinic_slug = f"{base_clinic_slug}-{counter}"
        counter += 1

    clinic_seats = 1
    if is_clinic_signup:
        from services.payment_service import PLAN_CONFIG
        clinic_seats = PLAN_CONFIG["clinic"]["seats"]   # matches the paid Clinic tier

    clinic = Clinic(
        # name.strip(): a trailing space in the signup field produced
        # "Rajesh Mehta 's Clinic" in the navbar switcher.
        name=clinic_name.strip() or f"{name.strip()}'s Clinic",
        address=None,
        city=city.strip() or None,
        slug=clinic_slug,
        plan_type="clinic" if is_clinic_signup else "trial",
        plan_expires_at=trial_ends_at if is_clinic_signup else None,
        max_doctors=clinic_seats,
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
    return clinic


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
    account_type: str = Form("solo"),
    also_own_practice: str = Form(""),
    db: Session = Depends(get_db),
):
    invite_token = clinic_invite.strip()

    # Normalise BEFORE any lookup so the duplicate check and the eventual
    # insert compare the exact same value.
    norm_email = _normalise_email(email)
    norm_phone = _normalise_phone(phone)

    def _reject(message: str, status_code: int = 400):
        # Re-resolve the invite so the retry form is still an INVITE form.
        # This passed joining_clinic=None unconditionally, so one typo turned
        # the page back into an ordinary signup: the "I also run my own
        # practice" choice disappeared and the Solo/Clinic selector took its
        # place. The invitee then had no way to say what they wanted, and the
        # account was effectively created for them.
        _joining = None
        _invite_email = None
        if invite_token:
            _inv = db.query(ClinicDoctorInvite).filter(
                ClinicDoctorInvite.token == invite_token,
                ClinicDoctorInvite.used_at == None,          # noqa: E711
                ClinicDoctorInvite.expires_at > datetime.utcnow(),
            ).first()
            if _inv:
                _joining = db.query(Clinic).filter(Clinic.id == _inv.clinic_id).first()
                _invite_email = _inv.email
        return templates.TemplateResponse(
            request, "register.html",
            {"error": message, "clinic_invite": invite_token,
             "joining_clinic": _joining, "plan_hint": "solo",
             "invite_email": _invite_email,
             # Keep the checkbox ticked if they had ticked it.
             "also_own_practice": bool(also_own_practice.strip())},
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

        # A token was supplied but didn't resolve. This used to fall through
        # to the solo branch, silently handing the user their own trial clinic
        # while they believed they were joining one — with no error shown.
        if not valid_invite:
            return _reject(
                "This clinic invite link is invalid or has expired. "
                "Ask the clinic to send you a new one."
            )

        # /clinic/doctor-invite enforces that the accepting doctor's email
        # matches the invite; registering did not, so the control was
        # bypassable by registering instead of logging in and accepting.
        # invite.email is lowercased when sent; norm_email is lowercased above.
        if valid_invite.email != norm_email:
            return _reject(
                f"This invite was sent to {valid_invite.email}. "
                f"Register with that email address, or ask the clinic to "
                f"re-send the invite to {norm_email}."
            )

    if valid_invite:
        # Seats are checked at ACCEPT time, not just when the invite is sent.
        # /clinic/doctor-invite already did this; registering did not, so an
        # invite issued while a seat was free could still be redeemed after
        # the clinic filled up — putting them over their paid headcount.
        _join_clinic = db.query(Clinic).filter(
            Clinic.id == valid_invite.clinic_id).first()
        _cap = (getattr(_join_clinic, "max_doctors", 1) or 1) if _join_clinic else 1
        _members = db.query(ClinicDoctor).filter(
            ClinicDoctor.clinic_id == valid_invite.clinic_id,
            ClinicDoctor.is_active == True,          # noqa: E712
        ).count()
        if _members >= _cap:
            return _reject(
                f"{_join_clinic.name if _join_clinic else 'This clinic'} has "
                f"reached its plan limit of {_cap} doctor(s). Ask the clinic "
                f"owner to upgrade — your invite link stays valid.",
                status_code=409,
            )

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

        # An invited doctor may also run their own practice — a real doctor
        # often does clinic shifts AND sees their own patients. Without this
        # they got no clinic and no trial at all, so they could never practise
        # independently; their only route was a second account on another
        # email. Their own practice is on the normal 14-day trial and is
        # billed separately from the clinic seat (access is per-clinic).
        if also_own_practice.strip():
            own_trial = datetime.utcnow() + timedelta(days=14)
            doctor.trial_ends_at = own_trial
            doctor.clinic_name = clinic_name.strip() or None
            db.commit()
            _create_owned_clinic(db, doctor, slug, name, clinic_name, city,
                                 False, own_trial)

    else:
        # ── Solo/clinic path: 14-day trial + auto clinic ───────────────────
        is_clinic_signup = account_type.strip().lower() == "clinic"
        trial_ends_at = datetime.utcnow() + timedelta(days=14)

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
            trial_ends_at=trial_ends_at,
            medical_reg_number=medical_reg_number.strip() or None,
        )
        db.add(doctor)
        db.commit()
        db.refresh(doctor)

        # Auto-create an implicit clinic for every doctor (owner role). A
        # "Clinic Account" signup gets Clinic Admin (invite doctors, manage
        # the team) for the trial window too — plan_expires_at mirrors the
        # doctor's own trial_ends_at, so it lapses at the same moment a solo
        # trial would, and the doctor sees the same upgrade prompt to keep it.
        # Previously account_type was accepted by the form but never read
        # here, so choosing "Clinic Account" silently produced an ordinary
        # solo account — plan_type stayed "trial" forever unless the doctor
        # separately paid for a duo/clinic/hospital/enterprise plan, which
        # is the only other place plan_type ever becomes "clinic"
        # (routers/doctors.py billing_verify).
        _create_owned_clinic(db, doctor, slug, name, clinic_name, city,
                             is_clinic_signup, trial_ends_at)

    # Send the verification code after the response — the Resend round-trip
    # must not delay the redirect. Passes the ID only; the wrapper opens its
    # own session (the request-scoped one is closed by then).
    from services.verification_service import issue_code_bg
    background_tasks.add_task(issue_code_bg, doctor.id)

    # Log the doctor straight in and land them on the verification screen.
    # Previously this redirected to /login?registered=1, so a new signup was
    # never told a code had been sent — the verification prompt only appeared
    # after they separately logged in, which made the whole step invisible.
    token = create_access_token({"doctor_id": doctor.id, "tv": doctor.token_version or 0})
    response = RedirectResponse(url="/verify-email", status_code=303)
    response.set_cookie(
        key="access_token", value=token,
        httponly=True, secure=settings.ENVIRONMENT.lower() == "production",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60, samesite="lax",
    )
    return response


# ------------------------------------------------------------------ #
#  Login                                                               #
# ------------------------------------------------------------------ #

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, registered: str = "", next: str = "", reset: str = ""):
    # Redirect already-logged-in users away from login
    token = request.cookies.get("access_token")
    if token and decode_token(token):
        return RedirectResponse(url="/dashboard", status_code=303)
    success = None
    if reset == "1":
        success = "Password updated. Log in with your new password."
    elif registered == "1":
        success = "Account created! Please log in."
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
        token = create_access_token({"doctor_id": doctor.id, "tv": doctor.token_version or 0})
        # Honor the `next` param — only relative paths, no open redirect
        safe_next = _safe_next(request, next)
        redirect_url = safe_next if safe_next else "/workspace-loading"
        response = RedirectResponse(url=redirect_url, status_code=303)
        response.set_cookie(
            key="access_token", value=token,
            httponly=True, secure=settings.ENVIRONMENT.lower() == "production", max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60, samesite="lax",
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
def logout(request: Request, next: str = Query(default="")):
    # Same open-redirect guard as /login's next= handling — relative paths
    # only. Lets a public page (e.g. a clinic invite link) send the doctor
    # straight back after switching accounts, instead of stranding them on
    # /login with no way back except retyping the invite URL from memory.
    safe_next = _safe_next(request, next)
    response = RedirectResponse(url=safe_next or "/login", status_code=303)
    # Clear EVERY auth cookie. Previously only access_token was deleted, so a
    # logged-out session left a live pin_session / clinic_admin_auth behind —
    # logging back in silently skipped the PIN gate.
    for cookie in ("access_token", "pin_session", "clinic_admin_auth", "active_clinic"):
        response.delete_cookie(cookie)
    return response


# ------------------------------------------------------------------ #
#  Email verification                                                  #
# ------------------------------------------------------------------ #
# Verification is MANDATORY — there is no skip option. get_paying_doctor()
# and get_appt_doctor() both call _require_verified() ahead of the plan
# check, so every protected route redirects here until the code is entered.
# Login itself still succeeds (a doctor needs a session to reach this page
# at all) but nothing past it is reachable while unverified.

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


# ------------------------------------------------------------------ #
#  Forgot / reset password                                             #
# ------------------------------------------------------------------ #

@router.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(request: Request):
    token = request.cookies.get("access_token")
    if token and decode_token(token):
        return RedirectResponse(url="/dashboard", status_code=303)
    return templates.TemplateResponse(request, "forgot_password.html", {
        "error": None, "sent": False,
    })


@router.post("/forgot-password", response_class=HTMLResponse)
def forgot_password_submit(
    request: Request,
    background_tasks: BackgroundTasks,
    email: str = Form(...),
):
    """Always renders the same confirmation.

    We never reveal whether the address is registered, verified, or rate
    limited — otherwise this endpoint becomes a probe for which doctors are
    on Med Track. The work runs in the background so response timing can't be
    used to infer it either.
    """
    from main import _client_ip
    from services.password_reset_service import request_reset_bg

    background_tasks.add_task(request_reset_bg, email, _client_ip(request))

    return templates.TemplateResponse(request, "forgot_password.html", {
        "error": None, "sent": True, "submitted_email": email.strip(),
    })


@router.get("/reset-password", response_class=HTMLResponse)
def reset_password_page(
    request: Request,
    token: str = Query(default=""),
    db: Session = Depends(get_db),
):
    from services.password_reset_service import validate_token

    record = validate_token(db, token)
    if not record:
        return templates.TemplateResponse(request, "reset_password.html", {
            "invalid": True, "token": "", "error": None,
        }, status_code=400)

    return templates.TemplateResponse(request, "reset_password.html", {
        "invalid": False, "token": token, "error": None,
    })


@router.post("/reset-password", response_class=HTMLResponse)
def reset_password_submit(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(""),
    db: Session = Depends(get_db),
):
    from services.password_reset_service import validate_token, consume_reset

    record = validate_token(db, token)
    if not record:
        return templates.TemplateResponse(request, "reset_password.html", {
            "invalid": True, "token": "", "error": None,
        }, status_code=400)

    if confirm_password and password != confirm_password:
        return templates.TemplateResponse(request, "reset_password.html", {
            "invalid": False, "token": token, "error": "Passwords don't match.",
        }, status_code=400)

    ok, message = consume_reset(db, record, password)
    if not ok:
        return templates.TemplateResponse(request, "reset_password.html", {
            "invalid": False, "token": token, "error": message,
        }, status_code=400)

    # Every prior session is dead (token_version bumped) — clear this
    # browser's cookies too so nothing stale lingers.
    response = RedirectResponse(url="/login?reset=1", status_code=303)
    for cookie in ("access_token", "pin_session", "clinic_admin_auth", "active_clinic"):
        response.delete_cookie(cookie)
    return response
