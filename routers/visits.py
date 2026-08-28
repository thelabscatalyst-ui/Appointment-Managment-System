"""
routers/visits.py — Today's Queue (v2 primary workflow)

Routes:
  GET  /visits/today                   Today's queue page
  GET  /visits/queue-status            JSON snapshot for polling (internal + display screen)
  POST /visits/check-in                Check in a walk-in patient
  POST /visits/{id}/call               Mark SERVING (manual call, skipping auto)
  POST /visits/{id}/done               Move to BILLING_PENDING, auto-call next
  POST /visits/{id}/skip               Skip to end of queue
  POST /visits/{id}/emergency          Promote to top (emergency)
  POST /visits/{id}/cancel             Mark cancelled
  POST /visits/{id}/move               Manual reorder (queue_position)

  GET  /queue/{slug}                   Public TV display screen
  GET  /queue/{slug}/status            Public polling JSON
"""

from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Request, Depends, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database.connection import get_db
from database.models import (
    Doctor, Patient, Appointment, AppointmentStatus, Visit, VisitStatus,
    ClinicDoctor, Bill, PaymentMode,
)
from services.auth_service import get_paying_doctor
from services.appointment_service import get_or_create_patient
import services.visit_service as vs

router = APIRouter(tags=["visits"])
templates = Jinja2Templates(directory="templates")


# --------------------------------------------------------------------------- #
#  Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _get_primary_clinic(doctor: Doctor, db: Session, request=None):
    """The clinic this work should be attributed to.

    Was an arbitrary `.first()` active membership with no role filter and no
    ORDER BY — for a two-clinic doctor it returned whichever row the database
    yielded first, so every visit and bill they ever created was stamped with
    that one clinic regardless of where the work happened. Now it follows the
    clinic the request is actually operating in.
    """
    from services.clinic_context import active_memberships
    from database.models import Clinic

    if request is not None:
        clinic = getattr(request.state, "active_clinic", None)
        if clinic is not None:
            return clinic
        cid = getattr(request.state, "active_clinic_id", None)
        if cid:
            return db.query(Clinic).filter(Clinic.id == cid).first()

    ms = active_memberships(db, doctor.id)   # owner-first, deterministic
    return ms[0].clinic if ms else None


# --------------------------------------------------------------------------- #
#  Today's Queue — main page                                                   #
# --------------------------------------------------------------------------- #

@router.get("/visits/today")
async def visits_today_redirect(request: Request):
    """Redirect old visits/today links to the unified appointments page."""
    return RedirectResponse("/appointments", status_code=301)


@router.get("/visits/today-view", response_class=HTMLResponse)
async def visits_today(
    request: Request,
    visit_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    doctor: Doctor = Depends(get_paying_doctor),
):
    target_date = date.today()
    if visit_date:
        try:
            target_date = date.fromisoformat(visit_date)
        except ValueError:
            pass

    serving, waiting, closed = vs.get_today_visits(db, doctor.id, target_date)
    primary_clinic = _get_primary_clinic(doctor, db, request)

    # Today's scheduled (not yet arrived) appointments — for the "Expected" strip
    scheduled_today = (
        db.query(Appointment)
        .filter(
            Appointment.doctor_id == doctor.id,
            Appointment.appointment_date == target_date,
            Appointment.visit_id.is_(None),
            Appointment.status == AppointmentStatus.scheduled,
        )
        .order_by(Appointment.appointment_time.asc())
        .all()
    )

    return templates.TemplateResponse(request, "visits_today.html", {
        "active":             "visits",
        "doctor":             doctor,
        "primary_clinic":     primary_clinic,
        "today":              target_date,
        "is_today":           target_date == date.today(),
        "serving":            serving,
        "waiting":            waiting,
        "closed":             closed,
        "scheduled_today":    scheduled_today,
        "total_today":        (1 if serving else 0) + len(waiting) + len(closed),
        "done_count":         len([v for v in closed if v.status == VisitStatus.done]),
    })


# --------------------------------------------------------------------------- #
#  Check-in (walk-in)                                                          #
# --------------------------------------------------------------------------- #

@router.post("/visits/check-in")
async def check_in_walkin(
    request: Request,
    name: str         = Form(...),
    phone: str        = Form(...),
    is_emergency: bool = Form(False),
    notes: str        = Form(""),
    db: Session       = Depends(get_db),
    doctor: Doctor    = Depends(get_paying_doctor),
):
    patient = get_or_create_patient(doctor.id, name.strip(), phone.strip(), db)
    primary_clinic = _get_primary_clinic(doctor, db, request)

    vs.check_in(
        db,
        doctor_id   = doctor.id,
        patient_id  = patient.id,
        clinic_id   = primary_clinic.id if primary_clinic else None,
        is_emergency = is_emergency,
        notes       = notes.strip() or None,
        created_by  = doctor.id,
    )
    return RedirectResponse("/appointments", status_code=303)


# --------------------------------------------------------------------------- #
#  Check-in from an existing appointment                                       #
# --------------------------------------------------------------------------- #

@router.post("/visits/check-in-appt/{appt_id}")
async def check_in_appointment(
    appt_id: int,
    request: Request,
    is_emergency: bool = Form(False),
    db: Session        = Depends(get_db),
    doctor: Doctor     = Depends(get_paying_doctor),
):
    appt = db.query(Appointment).filter(
        Appointment.id == appt_id,
        Appointment.doctor_id == doctor.id,
    ).first()
    if not appt:
        return RedirectResponse("/appointments", status_code=303)

    # Already checked in — check by visit table
    existing = db.query(Visit).filter(Visit.appointment_id == appt_id).first()
    if existing:
        return RedirectResponse("/appointments", status_code=303)

    primary_clinic = _get_primary_clinic(doctor, db, request)
    vs.check_in(
        db,
        doctor_id      = doctor.id,
        patient_id     = appt.patient_id,
        clinic_id      = primary_clinic.id if primary_clinic else None,
        appointment_id = appt.id,
        is_emergency   = is_emergency,
        created_by     = doctor.id,
    )
    return RedirectResponse("/appointments", status_code=303)


# --------------------------------------------------------------------------- #
#  Visit actions                                                               #
# --------------------------------------------------------------------------- #

def _get_visit(visit_id: int, doctor_id: int, db: Session) -> Optional[Visit]:
    return db.query(Visit).filter(
        Visit.id == visit_id,
        Visit.doctor_id == doctor_id,
    ).first()


def _auto_complete_appointment(db: Session, visit: Visit):
    """When a visit is marked done/billing, auto-complete the linked appointment."""
    if visit.appointment_id:
        appt = db.query(Appointment).filter(Appointment.id == visit.appointment_id).first()
        if appt and appt.status == AppointmentStatus.scheduled:
            appt.status = AppointmentStatus.completed


@router.post("/visits/{visit_id}/call")
async def call_visit(
    visit_id: int,
    request: Request,
    db: Session    = Depends(get_db),
    doctor: Doctor = Depends(get_paying_doctor),
):
    """Manually call a specific waiting visit (instead of auto call-next)."""
    visit = _get_visit(visit_id, doctor.id, db)
    if visit and visit.status == VisitStatus.waiting:
        visit.status    = VisitStatus.serving
        visit.call_time = datetime.now()
        db.commit()
    return RedirectResponse("/appointments", status_code=303)


@router.post("/visits/{visit_id}/done")
async def done_visit(
    visit_id: int,
    request: Request,
    db: Session    = Depends(get_db),
    doctor: Doctor = Depends(get_paying_doctor),
):
    """Mark serving visit as billing_pending, auto-call next, auto-complete appointment."""
    visit = _get_visit(visit_id, doctor.id, db)
    if visit and visit.status == VisitStatus.serving:
        _auto_complete_appointment(db, visit)
        db.commit()
        vs.done_and_call_next(db, visit)
    return RedirectResponse("/appointments", status_code=303)


@router.post("/visits/{visit_id}/hold")
async def hold_visit(
    visit_id: int,
    request: Request,
    db: Session    = Depends(get_db),
    doctor: Doctor = Depends(get_paying_doctor),
):
    """Put the serving patient on hold (x-ray/lab) and call the next patient."""
    visit = _get_visit(visit_id, doctor.id, db)
    if visit and visit.status == VisitStatus.serving:
        vs.hold_visit(db, visit)
    return RedirectResponse("/appointments", status_code=303)


@router.post("/visits/{visit_id}/resume")
async def resume_visit(
    visit_id: int,
    request: Request,
    db: Session    = Depends(get_db),
    doctor: Doctor = Depends(get_paying_doctor),
):
    """Bring an on-hold patient back — serve now if free, else next in queue."""
    visit = _get_visit(visit_id, doctor.id, db)
    if visit and visit.status == VisitStatus.on_hold:
        vs.resume_visit(db, visit)
    return RedirectResponse("/appointments", status_code=303)


@router.post("/visits/{visit_id}/close-free")
async def close_free(
    visit_id: int,
    request: Request,
    notes: str = Form(""),
    db: Session    = Depends(get_db),
    doctor: Doctor = Depends(get_paying_doctor),
):
    """Close a visit with no charge (free consultation). Creates a zero-value bill."""
    visit = _get_visit(visit_id, doctor.id, db)
    if not visit:
        return RedirectResponse("/appointments", status_code=303)

    primary_clinic = _get_primary_clinic(doctor, db, request)
    _auto_complete_appointment(db, visit)
    bill = Bill(
        visit_id     = visit.id,
        doctor_id    = doctor.id,
        clinic_id    = primary_clinic.id if primary_clinic else None,
        patient_id   = visit.patient_id,
        subtotal     = 0,
        discount     = 0,
        gst_amount   = 0,
        total        = 0,
        paid_amount  = 0,
        payment_mode = PaymentMode.free,
        paid_at      = datetime.now(),
        notes        = notes.strip() or None,
        created_by   = doctor.id,
    )
    db.add(bill)
    db.flush()
    vs.close_visit(db, visit, bill.id)
    db.commit()
    return RedirectResponse("/appointments", status_code=303)


@router.post("/visits/{visit_id}/skip")
async def skip_visit(
    visit_id: int,
    request: Request,
    db: Session    = Depends(get_db),
    doctor: Doctor = Depends(get_paying_doctor),
):
    visit = _get_visit(visit_id, doctor.id, db)
    if visit and visit.status in (VisitStatus.waiting, VisitStatus.serving):
        vs.skip_visit(db, visit)
    return RedirectResponse("/appointments", status_code=303)


@router.post("/visits/{visit_id}/emergency")
async def emergency_visit(
    visit_id: int,
    request: Request,
    db: Session    = Depends(get_db),
    doctor: Doctor = Depends(get_paying_doctor),
):
    visit = _get_visit(visit_id, doctor.id, db)
    if visit and visit.status == VisitStatus.waiting:
        vs.promote_emergency(db, visit)
    return RedirectResponse("/appointments", status_code=303)


@router.post("/visits/{visit_id}/cancel")
async def cancel_visit(
    visit_id: int,
    request: Request,
    db: Session    = Depends(get_db),
    doctor: Doctor = Depends(get_paying_doctor),
):
    visit = _get_visit(visit_id, doctor.id, db)
    if visit and visit.status in (VisitStatus.waiting, VisitStatus.serving, VisitStatus.on_hold):
        # Sync the linked appointment so it shows as Cancelled everywhere
        # (dashboard, appointment card, badges) instead of staying "scheduled".
        if visit.appointment_id:
            appt = db.query(Appointment).filter(Appointment.id == visit.appointment_id).first()
            if appt and appt.status == AppointmentStatus.scheduled:
                appt.status = AppointmentStatus.cancelled
                db.commit()
        vs.cancel_visit(db, visit)
    return RedirectResponse("/appointments", status_code=303)


@router.post("/visits/{visit_id}/move")
async def move_visit(
    visit_id: int,
    request: Request,
    new_position: int = Form(...),
    db: Session    = Depends(get_db),
    doctor: Doctor = Depends(get_paying_doctor),
):
    visit = _get_visit(visit_id, doctor.id, db)
    if visit and visit.status == VisitStatus.waiting:
        vs.move_visit(db, visit, new_position)
    return RedirectResponse("/appointments", status_code=303)


# --------------------------------------------------------------------------- #
#  Internal queue status JSON (for dashboard widget, etc.)                     #
# --------------------------------------------------------------------------- #

@router.get("/visits/queue-status", response_class=JSONResponse)
async def queue_status_internal(
    db: Session    = Depends(get_db),
    doctor: Doctor = Depends(get_paying_doctor),
):
    return vs.get_queue_status_json(db, doctor.id)


# --------------------------------------------------------------------------- #
#  Public display screen                                                       #
# --------------------------------------------------------------------------- #

@router.get("/queue/{slug}", response_class=HTMLResponse)
async def queue_display(
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
):
    doctor = db.query(Doctor).filter(Doctor.slug == slug).first()
    if not doctor:
        return HTMLResponse("<h2>Queue not found</h2>", status_code=404)

    status = vs.get_queue_status_json(db, doctor.id)
    return templates.TemplateResponse(request, "queue_display.html", {
        "doctor": doctor,
        "slug":   slug,
        "status": status,
    })


@router.get("/queue/{slug}/status", response_class=JSONResponse)
async def queue_status_public(
    slug: str,
    db: Session = Depends(get_db),
):
    doctor = db.query(Doctor).filter(Doctor.slug == slug).first()
    if not doctor:
        return JSONResponse({"error": "not found"}, status_code=404)
    return vs.get_queue_status_json(db, doctor.id)
