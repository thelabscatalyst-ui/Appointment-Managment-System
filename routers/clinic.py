"""
routers/clinic.py — Clinic admin routes (multi-doctor clinics only).

  /clinic/admin                 — clinic-owner dashboard (password-gated)
  /clinic/admin/auth            — password verification
  /clinic/admin/doctors         — manage doctors in the clinic
  /clinic/admin/doctors/invite  — send doctor invite
  /clinic/doctor-invite/{token} — accept doctor invite (public)
"""
import secrets
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Request, Depends, Form, Query, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database.connection import get_db
from database.models import (
    Clinic, ClinicDoctor, ClinicDoctorInvite,
    Doctor, Appointment, AppointmentStatus,
)
from config import settings
from services.auth_service import (
    get_clinic_owner, hash_password, verify_password, get_current_doctor,
)

router = APIRouter(prefix="/clinic", tags=["clinic"])
templates = Jinja2Templates(directory="templates")

ADMIN_AUTH_COOKIE = "clinic_admin_auth"


# ─────────────────────────────────────────────────────────────────────────── #
#  Helpers                                                                     #
# ─────────────────────────────────────────────────────────────────────────── #

def _get_clinic_doctors(clinic_id: int, db: Session) -> list[Doctor]:
    """Return active doctor objects for a clinic, ordered by name."""
    memberships = (
        db.query(ClinicDoctor)
        .filter(ClinicDoctor.clinic_id == clinic_id, ClinicDoctor.is_active == True)
        .all()
    )
    ids = [m.doctor_id for m in memberships]
    if not ids:
        return []
    return db.query(Doctor).filter(Doctor.id.in_(ids)).order_by(Doctor.name).all()


def _get_owner_clinic(doctor_id: int, db: Session) -> Clinic | None:
    """The clinic this doctor owns.

    Was a bare role=='owner' .first() with no is_active filter, so it could
    return a clinic whose membership had been deactivated — and could differ
    from the clinic get_clinic_owner had just validated.
    """
    from services.clinic_context import owned_clinic
    return owned_clinic(db, doctor_id)


def _is_admin_authenticated(request: Request, doctor_id: int) -> bool:
    """Returns True only if the short-lived clinic-admin cookie is valid for this doctor."""
    token = request.cookies.get(ADMIN_AUTH_COOKIE)
    if not token:
        return False
    from services.auth_service import decode_token
    payload = decode_token(token)
    return bool(payload and payload.get("clinic_admin") and payload.get("doctor_id") == doctor_id)


class ClinicAdminAuthRequired(Exception):
    """Owner is authenticated but has not passed the clinic-admin password gate.

    Raised as an exception rather than returned so it works from a dependency,
    which is what lets every /clinic/admin* route share one gate. Handled in
    main.py by rendering the password prompt.
    """
    pass


def require_clinic_admin(request: Request, doctor: Doctor = Depends(get_clinic_owner)):
    """Clinic owner AND past the password gate.

    The gate previously lived inline in the dashboard route only, so
    GET /clinic/admin/doctors and POST /clinic/admin/doctors/invite were
    reachable with just a live owner session — the roster could be listed and
    invites sent without ever re-entering the password. As a dependency it
    covers every admin route, including the lifecycle routes added later.
    """
    if not _is_admin_authenticated(request, doctor.id):
        raise ClinicAdminAuthRequired()
    return doctor


# ─────────────────────────────────────────────────────────────────────────── #
#  Clinic Admin — password gate                                                #
# ─────────────────────────────────────────────────────────────────────────── #

@router.post("/admin/auth", response_class=HTMLResponse)
def clinic_admin_auth(
    request: Request,
    password: str = Form(...),
    doctor: Doctor = Depends(get_clinic_owner),
):
    """Verify doctor's login password → set short-lived clinic-admin cookie."""
    if not verify_password(password, doctor.password_hash):
        response = RedirectResponse(url="/clinic/admin?auth_error=1", status_code=303)
        return response
    from datetime import timedelta as td
    from jose import jwt as _jwt
    from config import settings
    import time
    payload = {
        "doctor_id":   doctor.id,
        "clinic_admin": True,
        "exp":          int(time.time()) + 600,   # 10 min
    }
    token = _jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    response = RedirectResponse(url="/clinic/admin", status_code=303)
    response.set_cookie(ADMIN_AUTH_COOKIE, token, httponly=True, secure=settings.ENVIRONMENT.lower() == "production", samesite="lax", max_age=600)
    return response


# ─────────────────────────────────────────────────────────────────────────── #
#  Clinic Admin Dashboard                                                      #
# ─────────────────────────────────────────────────────────────────────────── #

@router.get("/admin", response_class=HTMLResponse)
def clinic_admin_dashboard(
    request: Request,
    doctor: Doctor = Depends(get_clinic_owner),
    db: Session = Depends(get_db),
):
    clinic = _get_owner_clinic(doctor.id, db)
    if not clinic:
        return RedirectResponse(url="/dashboard", status_code=303)

    # Password gate — kept inline here (rather than via require_clinic_admin)
    # so the prompt renders in place with its auth_error, which is the entry
    # point users actually land on. Sibling routes use the dependency.
    if not _is_admin_authenticated(request, doctor.id):
        return templates.TemplateResponse(request, "clinic/admin_auth.html", {
            "doctor":     doctor,
            "auth_error": request.query_params.get("auth_error"),
        })

    today      = date.today()
    week_start = today - timedelta(days=today.weekday())
    doctors    = _get_clinic_doctors(clinic.id, db)

    doctor_stats = []
    for d in doctors:
        # Scoped to THIS clinic. Without the clinic filter these counted an
        # associate's shifts at other clinics — including a competitor's —
        # into this owner's totals.
        today_count = db.query(Appointment).filter(
            Appointment.doctor_id == d.id,
            Appointment.clinic_id == clinic.id,
            Appointment.appointment_date == today,
            Appointment.status != AppointmentStatus.cancelled,
        ).count()
        week_count = db.query(Appointment).filter(
            Appointment.doctor_id == d.id,
            Appointment.clinic_id == clinic.id,
            Appointment.appointment_date >= week_start,
            Appointment.appointment_date <= today,
            Appointment.status != AppointmentStatus.cancelled,
        ).count()
        membership = db.query(ClinicDoctor).filter(
            ClinicDoctor.doctor_id == d.id,
            ClinicDoctor.clinic_id == clinic.id,
        ).first()
        doctor_stats.append({
            "doctor": d,
            "today":  today_count,
            "week":   week_count,
            "role":   membership.role if membership else "associate",
        })

    return templates.TemplateResponse(request, "clinic/admin_dashboard.html", {
        "doctor":       doctor,
        "clinic":       clinic,
        "doctor_stats": doctor_stats,
        "total_today":  sum(s["today"] for s in doctor_stats),
        "active":       "clinic_admin",
    })


# ─────────────────────────────────────────────────────────────────────────── #
#  Doctor Management                                                           #
# ─────────────────────────────────────────────────────────────────────────── #

@router.get("/admin/doctors", response_class=HTMLResponse)
def doctors_list_page(
    request: Request,
    doctor: Doctor = Depends(require_clinic_admin),
    db: Session = Depends(get_db),
):
    clinic = _get_owner_clinic(doctor.id, db)
    if not clinic:
        return RedirectResponse(url="/dashboard", status_code=303)

    memberships = db.query(ClinicDoctor).filter(ClinicDoctor.clinic_id == clinic.id).all()
    clinic_doctors = []
    for m in memberships:
        d = db.query(Doctor).filter(Doctor.id == m.doctor_id).first()
        if d:
            clinic_doctors.append({
                "doctor":        d,
                "role":          m.role,
                "is_active":     m.is_active,
                "membership_id": m.id,
            })

    pending_invites = (
        db.query(ClinicDoctorInvite)
        .filter(
            ClinicDoctorInvite.clinic_id == clinic.id,
            ClinicDoctorInvite.used_at   == None,
            ClinicDoctorInvite.expires_at > datetime.utcnow(),
        )
        .all()
    )

    return templates.TemplateResponse(request, "clinic/admin_doctors.html", {
        "doctor":          doctor,
        "clinic":          clinic,
        "clinic_doctors":  clinic_doctors,
        "pending_invites": pending_invites,
        "active":          "clinic_admin",
        "success":         None,
        "error":           None,
    })


@router.post("/admin/doctors/invite", response_class=HTMLResponse)
def send_doctor_invite(
    request: Request,
    background_tasks: BackgroundTasks,
    invite_email: str = Form(...),
    doctor: Doctor = Depends(require_clinic_admin),
    db: Session = Depends(get_db),
):
    clinic = _get_owner_clinic(doctor.id, db)
    if not clinic:
        return RedirectResponse(url="/dashboard", status_code=303)

    email = invite_email.lower().strip()

    def _render(success=None, error=None):
        memberships = db.query(ClinicDoctor).filter(ClinicDoctor.clinic_id == clinic.id).all()
        clinic_doctors = []
        for m in memberships:
            d = db.query(Doctor).filter(Doctor.id == m.doctor_id).first()
            if d:
                clinic_doctors.append({"doctor": d, "role": m.role,
                                       "is_active": m.is_active, "membership_id": m.id})
        pending_invites = db.query(ClinicDoctorInvite).filter(
            ClinicDoctorInvite.clinic_id == clinic.id,
            ClinicDoctorInvite.used_at   == None,
            ClinicDoctorInvite.expires_at > datetime.utcnow(),
        ).all()
        return templates.TemplateResponse(
            request, "clinic/admin_doctors.html",
            {"doctor": doctor, "clinic": clinic, "clinic_doctors": clinic_doctors,
             "pending_invites": pending_invites, "active": "clinic_admin",
             "success": success, "error": error},
            status_code=400 if error else 200,
        )

    # Plan limit check
    active_doctor_count = db.query(ClinicDoctor).filter(
        ClinicDoctor.clinic_id == clinic.id, ClinicDoctor.is_active == True
    ).count()
    max_doctors = getattr(clinic, "max_doctors", 1) or 1
    if active_doctor_count >= max_doctors:
        plan_label = "Solo plan (single doctor)" if max_doctors <= 1 else f"current plan (max {max_doctors} doctors)"
        return _render(error=f"Doctor limit reached for your {plan_label}. Upgrade to Clinic plan to add more doctors.")

    existing_doctor = db.query(Doctor).filter(Doctor.email == email).first()
    if existing_doctor:
        already = db.query(ClinicDoctor).filter(
            ClinicDoctor.clinic_id == clinic.id,
            ClinicDoctor.doctor_id == existing_doctor.id,
        ).first()
        if already:
            return _render(error=f"{email} is already a doctor in this clinic.")

    # Revoke any existing unused invite
    db.query(ClinicDoctorInvite).filter(
        ClinicDoctorInvite.clinic_id == clinic.id,
        ClinicDoctorInvite.email     == email,
        ClinicDoctorInvite.used_at   == None,
    ).delete()
    db.commit()

    token = secrets.token_urlsafe(32)
    db.add(ClinicDoctorInvite(
        clinic_id  = clinic.id,
        email      = email,
        token      = token,
        expires_at = datetime.utcnow() + timedelta(days=7),
    ))
    db.commit()

    # Send after the response so a slow/unreachable mail provider can't stall
    # the owner's request. send_invite_email never raises; it logs the accept
    # URL if delivery fails so the link can be shared manually.
    from services.invite_service import send_invite_email
    background_tasks.add_task(send_invite_email, email, token, clinic.name, doctor.name)

    return _render(success=f"Invite sent to {email}. They have 7 days to accept.")


# ─────────────────────────────────────────────────────────────────────────── #
#  Doctor Invite Accept — public                                               #
# ─────────────────────────────────────────────────────────────────────────── #

@router.get("/doctor-invite/{token}", response_class=HTMLResponse)
def doctor_invite_page(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
):
    invite = db.query(ClinicDoctorInvite).filter(ClinicDoctorInvite.token == token).first()
    if not invite or invite.used_at or invite.expires_at < datetime.utcnow():
        return templates.TemplateResponse(request, "clinic/invite_invalid.html", {
            "reason": "This invite link is invalid or has expired."
        }, status_code=410)

    clinic = db.query(Clinic).filter(Clinic.id == invite.clinic_id).first()

    logged_in_doctor = None
    token_cookie = request.cookies.get("access_token")
    if token_cookie:
        try:
            from jose import jwt
            from config import settings
            payload = jwt.decode(token_cookie, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
            doctor_id = payload.get("doctor_id")
            if doctor_id:
                logged_in_doctor = db.query(Doctor).filter(Doctor.id == doctor_id).first()
        except Exception:
            pass

    already_member = False
    if logged_in_doctor:
        already_member = db.query(ClinicDoctor).filter(
            ClinicDoctor.clinic_id == invite.clinic_id,
            ClinicDoctor.doctor_id == logged_in_doctor.id,
        ).first() is not None

    return templates.TemplateResponse(request, "clinic/doctor_invite.html", {
        "invite":           invite,
        "clinic":           clinic,
        "logged_in_doctor": logged_in_doctor,
        "already_member":   already_member,
        "error":            None,
    })


@router.post("/doctor-invite/{token}", response_class=HTMLResponse)
def doctor_invite_accept(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
):
    invite = db.query(ClinicDoctorInvite).filter(ClinicDoctorInvite.token == token).first()
    if not invite or invite.used_at or invite.expires_at < datetime.utcnow():
        return templates.TemplateResponse(request, "clinic/invite_invalid.html", {
            "reason": "This invite link is invalid or has expired."
        }, status_code=410)

    clinic = db.query(Clinic).filter(Clinic.id == invite.clinic_id).first()

    logged_in_doctor = None
    token_cookie = request.cookies.get("access_token")
    if token_cookie:
        try:
            from jose import jwt
            from config import settings
            payload = jwt.decode(token_cookie, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
            doctor_id = payload.get("doctor_id")
            if doctor_id:
                logged_in_doctor = db.query(Doctor).filter(Doctor.id == doctor_id).first()
        except Exception:
            pass

    if not logged_in_doctor:
        return templates.TemplateResponse(request, "clinic/doctor_invite.html", {
            "invite": invite, "clinic": clinic,
            "logged_in_doctor": None, "already_member": False,
            "error": "Please log in first, then come back to this link.",
        })

    already = db.query(ClinicDoctor).filter(
        ClinicDoctor.clinic_id == invite.clinic_id,
        ClinicDoctor.doctor_id == logged_in_doctor.id,
    ).first()
    if already:
        return templates.TemplateResponse(request, "clinic/doctor_invite.html", {
            "invite": invite, "clinic": clinic,
            "logged_in_doctor": logged_in_doctor, "already_member": True,
            "error": "You are already a member of this clinic.",
        })

    # Server-side enforcement of the same check the template guards against —
    # a direct POST could otherwise let whoever is logged in accept an invite
    # addressed to someone else, silently burning it for the real invitee.
    if logged_in_doctor.email != invite.email:
        return templates.TemplateResponse(request, "clinic/doctor_invite.html", {
            "invite": invite, "clinic": clinic,
            "logged_in_doctor": logged_in_doctor, "already_member": False,
            "error": f"This invite was sent to {invite.email}, not {logged_in_doctor.email}.",
        }, status_code=403)

    db.add(ClinicDoctor(
        clinic_id = invite.clinic_id,
        doctor_id = logged_in_doctor.id,
        role      = "associate",
        is_active = True,
    ))
    invite.used_at = datetime.utcnow()
    db.commit()

    return RedirectResponse(url="/dashboard?joined=1", status_code=303)


# ─────────────────────────────────────────────────────────────────────────── #
#  Active clinic switching                                                     #
# ─────────────────────────────────────────────────────────────────────────── #

@router.post("/switch")
def switch_clinic(
    request: Request,
    clinic_id: int = Form(...),
    next: str = Form(default="/dashboard"),
    doctor: Doctor = Depends(get_current_doctor),
    db: Session = Depends(get_db),
):
    """Change which clinic the doctor is working in.

    Deliberately depends on get_current_doctor, NOT get_paying_doctor. If this
    were plan-gated, a doctor whose personal plan lapsed while their active
    clinic is their own practice would be redirected to /billing on every
    request — including this one — and could never switch back to the paid
    clinic where they still have legitimate access.

    The requested clinic is validated against live membership; a stale or
    forged id simply leaves the context unchanged.
    """
    from services.clinic_context import get_membership, set_active_clinic_cookie

    # Same open-redirect guard the logout route uses: only site-relative paths.
    safe_next = next if next.startswith("/") and not next.startswith("//") else "/dashboard"

    if not get_membership(db, doctor.id, clinic_id):
        return RedirectResponse(url=safe_next, status_code=303)

    response = RedirectResponse(url=safe_next, status_code=303)
    set_active_clinic_cookie(response, doctor.id, clinic_id)
    # Switching clinics changes what the admin gate applies to, so make them
    # re-authenticate rather than carrying a cookie minted for the old clinic.
    response.delete_cookie(ADMIN_AUTH_COOKIE)
    return response
