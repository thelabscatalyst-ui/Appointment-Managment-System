from datetime import date, time, datetime, timedelta
from fastapi import APIRouter, Request, Depends, Form, Query, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database.connection import get_db
from database.models import (
    Doctor, Clinic, ClinicDoctor, Appointment, Patient,
    AppointmentStatus, AppointmentType, BookedBy,
)
from services.appointment_service import (
    get_available_slots, is_slot_available, get_or_create_patient,
    has_open_appointment_on_date,
)
from services.notification_service import notify_appointment_confirmed

router = APIRouter(prefix="/book", tags=["public"])
templates = Jinja2Templates(directory="templates")


def _rate_limit_ok(phone: str, db: Session) -> bool:
    """Allow max 5 patient-booked appointments per phone number per 24 hours."""
    cutoff = datetime.utcnow() - timedelta(hours=24)
    count = (
        db.query(Appointment)
        .join(Patient, Appointment.patient_id == Patient.id)
        .filter(
            Patient.phone == phone,
            Appointment.booked_by == BookedBy.patient,
            Appointment.created_at >= cutoff,
        )
        .count()
    )
    return count < 5


# ------------------------------------------------------------------ #
#  Clinic public booking — Step 5                                      #
# ------------------------------------------------------------------ #

@router.get("/clinic/{slug}/slots")
def clinic_public_slots(
    slug: str,
    date_str: str = Query(..., alias="date"),
    doctor_id: int = Query(...),
    db: Session = Depends(get_db),
):
    clinic = db.query(Clinic).filter(Clinic.slug == slug).first()
    if not clinic:
        return JSONResponse({"slots": []})
    # Verify doctor belongs to this clinic
    membership = db.query(ClinicDoctor).filter(
        ClinicDoctor.clinic_id == clinic.id,
        ClinicDoctor.doctor_id == doctor_id,
        ClinicDoctor.is_active == True,
    ).first()
    if not membership:
        return JSONResponse({"slots": []})
    try:
        appt_date = date.fromisoformat(date_str)
    except ValueError:
        return JSONResponse({"slots": []})
    return JSONResponse({"slots": get_available_slots(doctor_id, appt_date, db)})


@router.get("/clinic/{slug}", response_class=HTMLResponse)
def clinic_booking_page(
    slug: str,
    request: Request,
    selected_doctor_id: int = Query(default=0),
    db: Session = Depends(get_db),
):
    clinic = db.query(Clinic).filter(Clinic.slug == slug).first()
    if not clinic:
        return templates.TemplateResponse(
            request, "public_clinic_booking.html",
            {"clinic": None, "not_found": True},
            status_code=404,
        )
    memberships = db.query(ClinicDoctor).filter(
        ClinicDoctor.clinic_id == clinic.id, ClinicDoctor.is_active == True,
    ).all()
    doctor_ids = [m.doctor_id for m in memberships]
    doctors = db.query(Doctor).filter(Doctor.id.in_(doctor_ids), Doctor.is_active == True).order_by(Doctor.name).all()

    selected = next((d for d in doctors if d.id == selected_doctor_id), None)
    today = date.today()
    slots = get_available_slots(selected.id, today, db) if selected else []

    return templates.TemplateResponse(request, "public_clinic_booking.html", {
        "clinic": clinic,
        "not_found": False,
        "doctors": doctors,
        "selected_doctor": selected,
        "today": today.isoformat(),
        "slots": slots,
        "appointment_types": [e.value for e in AppointmentType],
        "error": None,
        "form_data": {},
    })


@router.post("/clinic/{slug}", response_class=HTMLResponse)
async def clinic_book_appointment(
    slug: str,
    request: Request,
    background_tasks: BackgroundTasks,
    doctor_id: int = Form(...),
    patient_name: str = Form(...),
    patient_phone: str = Form(...),
    appt_date: str = Form(...),
    appt_time: str = Form(...),
    appointment_type: str = Form("new_patient"),
    patient_notes: str = Form(""),
    referral_source: str = Form(""),
    referral_source_other: str = Form(""),
    db: Session = Depends(get_db),
):
    clinic = db.query(Clinic).filter(Clinic.slug == slug).first()
    if not clinic:
        return RedirectResponse(url="/", status_code=303)

    memberships = db.query(ClinicDoctor).filter(
        ClinicDoctor.clinic_id == clinic.id, ClinicDoctor.is_active == True,
    ).all()
    doctor_ids = [m.doctor_id for m in memberships]
    doctors = db.query(Doctor).filter(Doctor.id.in_(doctor_ids), Doctor.is_active == True).order_by(Doctor.name).all()
    selected = next((d for d in doctors if d.id == doctor_id), None)
    today = date.today()

    def render_error(msg: str):
        try:
            d = date.fromisoformat(appt_date)
        except (ValueError, TypeError):
            d = today
        slots = get_available_slots(selected.id, d, db) if selected else []
        return templates.TemplateResponse(request, "public_clinic_booking.html", {
            "clinic": clinic, "not_found": False,
            "doctors": doctors, "selected_doctor": selected,
            "today": today.isoformat(), "slots": slots,
            "appointment_types": [e.value for e in AppointmentType],
            "error": msg,
            "form_data": {"doctor_id": doctor_id, "patient_name": patient_name,
                          "patient_phone": patient_phone, "appt_date": appt_date,
                          "appt_time": appt_time, "appointment_type": appointment_type},
        })

    if not selected:
        return render_error("Invalid doctor selection.")

    try:
        appt_date_obj = date.fromisoformat(appt_date)
        appt_time_obj = time.fromisoformat(appt_time)
    except ValueError:
        return render_error("Invalid date or time. Please select a valid slot.")

    name  = patient_name.strip()
    phone = patient_phone.strip()
    if not name:
        return render_error("Please enter your full name.")
    if not phone or not phone.isdigit() or len(phone) != 10:
        return render_error("Please enter a valid 10-digit phone number.")

    if not _rate_limit_ok(phone, db):
        return render_error("Too many bookings from this number in the last 24 hours. Please call the clinic directly.")

    # Duplicate open appointment check
    if has_open_appointment_on_date(selected.id, phone, appt_date_obj, db):
        return render_error(
            "You already have a scheduled appointment on this day. "
            "Please contact the clinic to reschedule or cancel it first."
        )

    ok, reason = is_slot_available(selected.id, appt_date_obj, appt_time_obj, db)
    if not ok:
        return render_error(reason)

    patient = get_or_create_patient(
        selected.id, name, phone, db,
        referral_source=(referral_source.strip() or None),
        referral_source_other=(referral_source_other.strip() or None),
    )

    try:
        appt_type = AppointmentType(appointment_type)
    except ValueError:
        appt_type = AppointmentType.new_patient

    appt = Appointment(
        doctor_id        = selected.id,
        patient_id       = patient.id,
        clinic_id        = clinic.id,
        appointment_date = appt_date_obj,
        appointment_time = appt_time_obj,
        duration_mins    = 15,
        appointment_type = appt_type,
        patient_notes    = patient_notes.strip() or None,
        booked_by        = BookedBy.patient,
        status           = AppointmentStatus.scheduled,
    )
    db.add(appt)

    if patient.first_visit is None:
        patient.first_visit = appt_date_obj
    patient.last_visit  = appt_date_obj
    patient.visit_count = (patient.visit_count or 0) + 1

    db.commit()
    db.refresh(appt)

    from services.notification_service import send_appointment_confirmed_bg
    background_tasks.add_task(send_appointment_confirmed_bg, appt.id, selected.id)

    return RedirectResponse(url=f"/book/clinic/{slug}/confirm/{appt.id}", status_code=303)


@router.get("/clinic/{slug}/confirm/{appt_id}", response_class=HTMLResponse)
def clinic_booking_confirm(
    slug: str,
    appt_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    clinic = db.query(Clinic).filter(Clinic.slug == slug).first()
    if not clinic:
        return RedirectResponse(url=f"/book/clinic/{slug}", status_code=303)

    appt = db.query(Appointment).filter(
        Appointment.id == appt_id, Appointment.clinic_id == clinic.id,
    ).first()
    if not appt:
        return RedirectResponse(url=f"/book/clinic/{slug}", status_code=303)

    doctor = db.query(Doctor).filter(Doctor.id == appt.doctor_id).first()
    _ = appt.patient  # lazy-load

    dt_start = datetime.combine(appt.appointment_date, appt.appointment_time)
    dt_end   = dt_start + timedelta(minutes=appt.duration_mins)
    gc_clinic = clinic.name.replace(" ", "+")
    gc_url = (
        "https://calendar.google.com/calendar/render?action=TEMPLATE"
        f"&text=Appointment+at+{gc_clinic}"
        f"&dates={dt_start.strftime('%Y%m%dT%H%M%S')}/{dt_end.strftime('%Y%m%dT%H%M%S')}"
        f"&details=Appointment+with+Dr.+{doctor.name.replace(' ', '+')}"
        + (f"&location={clinic.address.replace(' ', '+')}" if clinic.address else "")
    )

    return templates.TemplateResponse(request, "public_clinic_confirm.html", {
        "clinic": clinic,
        "doctor": doctor,
        "appt":   appt,
        "gc_url": gc_url,
        "slug":   slug,
    })


# ------------------------------------------------------------------ #
#  Public slots — AJAX (no auth, keyed by slug)                       #
# ------------------------------------------------------------------ #

@router.get("/{slug}/slots")
def public_slots(
    slug: str,
    date_str: str = Query(..., alias="date"),
    db: Session = Depends(get_db),
):
    doctor = db.query(Doctor).filter(
        Doctor.slug == slug, Doctor.is_active == True,
    ).first()
    if not doctor:
        return JSONResponse({"slots": []})
    try:
        appt_date = date.fromisoformat(date_str)
    except ValueError:
        return JSONResponse({"slots": []})
    return JSONResponse({"slots": get_available_slots(doctor.id, appt_date, db)})


# ------------------------------------------------------------------ #
#  Booking Form — GET                                                  #
# ------------------------------------------------------------------ #

@router.get("/{slug}", response_class=HTMLResponse)
def booking_page(
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
):
    doctor = db.query(Doctor).filter(
        Doctor.slug == slug, Doctor.is_active == True,
    ).first()

    if not doctor:
        return templates.TemplateResponse(
            request, "public_booking.html",
            {"doctor": None, "not_found": True},
            status_code=404,
        )

    today = date.today()
    slots = get_available_slots(doctor.id, today, db)

    return templates.TemplateResponse(request, "public_booking.html", {
        "doctor": doctor,
        "not_found": False,
        "today": today.isoformat(),
        "initial_date": today.isoformat(),
        "slots": slots,
        "appointment_types": [e.value for e in AppointmentType],
        "error": None,
        "form_data": {},
    })


# ------------------------------------------------------------------ #
#  Booking Form — POST                                                 #
# ------------------------------------------------------------------ #

@router.post("/{slug}", response_class=HTMLResponse)
async def book_appointment(
    slug: str,
    request: Request,
    background_tasks: BackgroundTasks,
    patient_name: str = Form(...),
    patient_phone: str = Form(...),
    appt_date: str = Form(...),
    appt_time: str = Form(...),
    appointment_type: str = Form("new_patient"),
    patient_notes: str = Form(""),
    referral_source: str = Form(""),
    referral_source_other: str = Form(""),
    db: Session = Depends(get_db),
):
    doctor = db.query(Doctor).filter(
        Doctor.slug == slug, Doctor.is_active == True,
    ).first()
    if not doctor:
        return RedirectResponse(url="/", status_code=303)

    today = date.today()
    form_data = {
        "patient_name": patient_name,
        "patient_phone": patient_phone,
        "appt_date": appt_date,
        "appt_time": appt_time,
        "appointment_type": appointment_type,
        "patient_notes": patient_notes,
    }

    def render_error(msg: str):
        try:
            d = date.fromisoformat(appt_date)
        except (ValueError, TypeError):
            d = today
        slots = get_available_slots(doctor.id, d, db)
        return templates.TemplateResponse(request, "public_booking.html", {
            "doctor": doctor,
            "not_found": False,
            "today": today.isoformat(),
            "initial_date": appt_date,
            "slots": slots,
            "appointment_types": [e.value for e in AppointmentType],
            "error": msg,
            "form_data": form_data,
        })

    # Parse date / time
    try:
        appt_date_obj = date.fromisoformat(appt_date)
        appt_time_obj = time.fromisoformat(appt_time)
    except ValueError:
        return render_error("Invalid date or time. Please select a valid slot.")

    # Validate patient fields
    name  = patient_name.strip()
    phone = patient_phone.strip()
    if not name:
        return render_error("Please enter your full name.")
    if not phone or not phone.isdigit() or len(phone) != 10:
        return render_error("Please enter a valid 10-digit phone number.")

    # Rate limit
    if not _rate_limit_ok(phone, db):
        return render_error(
            "Too many bookings from this number in the last 24 hours. "
            "Please call the clinic directly."
        )

    # Duplicate open appointment check
    if has_open_appointment_on_date(doctor.id, phone, appt_date_obj, db):
        return render_error(
            "You already have a scheduled appointment on this day. "
            "Please contact the clinic to reschedule or cancel it first."
        )

    # Slot availability
    ok, reason = is_slot_available(doctor.id, appt_date_obj, appt_time_obj, db)
    if not ok:
        return render_error(reason)

    # Get or create patient
    patient = get_or_create_patient(
        doctor.id, name, phone, db,
        referral_source=(referral_source.strip() or None),
        referral_source_other=(referral_source_other.strip() or None),
    )

    try:
        appt_type = AppointmentType(appointment_type)
    except ValueError:
        appt_type = AppointmentType.new_patient

    # Create appointment
    # This is the doctor's personal booking link, so the appointment belongs to
    # the practice they own — not to a clinic they merely do shifts at.
    from services.clinic_context import owned_clinic, active_memberships
    _own = owned_clinic(db, doctor.id)
    if _own is None:
        _ms = active_memberships(db, doctor.id)
        _own_id = _ms[0].clinic_id if _ms else None
    else:
        _own_id = _own.id

    appt = Appointment(
        doctor_id=doctor.id,
        patient_id=patient.id,
        clinic_id=_own_id,
        appointment_date=appt_date_obj,
        appointment_time=appt_time_obj,
        duration_mins=15,
        appointment_type=appt_type,
        patient_notes=patient_notes.strip() or None,
        booked_by=BookedBy.patient,
        status=AppointmentStatus.scheduled,
    )
    db.add(appt)

    if patient.first_visit is None:
        patient.first_visit = appt_date_obj
    patient.last_visit = appt_date_obj
    patient.visit_count = (patient.visit_count or 0) + 1

    db.commit()
    db.refresh(appt)

    # Send WhatsApp confirmation to patient AFTER the response (non-blocking)
    from services.notification_service import send_appointment_confirmed_bg
    background_tasks.add_task(send_appointment_confirmed_bg, appt.id, doctor.id)

    return RedirectResponse(url=f"/book/{slug}/confirm/{appt.id}", status_code=303)


# ------------------------------------------------------------------ #
#  Confirmation — GET                                                  #
# ------------------------------------------------------------------ #

@router.get("/{slug}/confirm/{appt_id}", response_class=HTMLResponse)
def booking_confirm(
    slug: str,
    appt_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    doctor = db.query(Doctor).filter(Doctor.slug == slug).first()
    if not doctor:
        return RedirectResponse(url=f"/book/{slug}", status_code=303)

    appt = db.query(Appointment).filter(
        Appointment.id == appt_id,
        Appointment.doctor_id == doctor.id,
    ).first()
    if not appt:
        return RedirectResponse(url=f"/book/{slug}", status_code=303)

    appt.patient  # lazy-load

    # Build Google Calendar add link
    dt_start = datetime.combine(appt.appointment_date, appt.appointment_time)
    dt_end   = dt_start + timedelta(minutes=appt.duration_mins)
    clinic   = (doctor.clinic_name or f"Dr. {doctor.name}").replace(" ", "+")
    gc_url = (
        "https://calendar.google.com/calendar/render?action=TEMPLATE"
        f"&text=Appointment+at+{clinic}"
        f"&dates={dt_start.strftime('%Y%m%dT%H%M%S')}/{dt_end.strftime('%Y%m%dT%H%M%S')}"
        f"&details=Appointment+with+Dr.+{doctor.name.replace(' ', '+')}"
        + (f"&location={doctor.clinic_address.replace(' ', '+')}" if doctor.clinic_address else "")
    )

    return templates.TemplateResponse(request, "public_confirm.html", {
        "doctor": doctor,
        "appt":   appt,
        "gc_url": gc_url,
        "slug":   slug,
    })
