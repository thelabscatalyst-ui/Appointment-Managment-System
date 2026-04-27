from datetime import date, time, timedelta, datetime
from fastapi import APIRouter, Request, Depends, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database.connection import get_db
from database.models import (
    Doctor, Patient, Appointment, AppointmentStatus, AppointmentType, BookedBy,
    ClinicDoctor, Clinic,
)
from services.auth_service import get_paying_doctor
from services.appointment_service import (
    get_available_slots, is_slot_available, is_slot_available_for_edit,
    get_or_create_patient, has_open_appointment_on_date,
)
from services.notification_service import notify_appointment_confirmed

router = APIRouter(prefix="/appointments", tags=["appointments"])
templates = Jinja2Templates(directory="templates")


def _get_owner_clinic_doctors(doctor: Doctor, db: Session) -> list[Doctor]:
    """If doctor is a clinic owner with multiple doctors, return all of them. Else empty."""
    ownership = db.query(ClinicDoctor).filter(
        ClinicDoctor.doctor_id == doctor.id, ClinicDoctor.role == "owner"
    ).first()
    if not ownership:
        return []
    members = db.query(ClinicDoctor).filter(
        ClinicDoctor.clinic_id == ownership.clinic_id, ClinicDoctor.is_active == True
    ).all()
    if len(members) < 2:
        return []  # solo clinic, no selector needed
    ids = [m.doctor_id for m in members]
    return db.query(Doctor).filter(Doctor.id.in_(ids)).order_by(Doctor.name).all()


def _resolve_target_doctor(for_doctor_id: int, logged_in_doctor: Doctor, db: Session) -> Doctor:
    """Return the target doctor if the logged-in doctor is their clinic owner, else return logged_in_doctor."""
    if not for_doctor_id or for_doctor_id == logged_in_doctor.id:
        return logged_in_doctor
    ownership = db.query(ClinicDoctor).filter(
        ClinicDoctor.doctor_id == logged_in_doctor.id, ClinicDoctor.role == "owner"
    ).first()
    if not ownership:
        return logged_in_doctor
    member = db.query(ClinicDoctor).filter(
        ClinicDoctor.clinic_id == ownership.clinic_id,
        ClinicDoctor.doctor_id == for_doctor_id,
        ClinicDoctor.is_active == True,
    ).first()
    if not member:
        return logged_in_doctor
    target = db.query(Doctor).filter(Doctor.id == for_doctor_id).first()
    return target or logged_in_doctor


# ------------------------------------------------------------------ #
#  List                                                                #
# ------------------------------------------------------------------ #

@router.get("", response_class=HTMLResponse)
def appointments_list(
    request: Request,
    filter_date: str = Query(default=""),
    doctor_id: int = Query(default=0),
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    today = date.today()
    try:
        view_date = date.fromisoformat(filter_date) if filter_date else today
    except ValueError:
        view_date = today

    clinic_doctors = _get_owner_clinic_doctors(doctor, db)
    viewing_doctor = _resolve_target_doctor(doctor_id, doctor, db)

    appointments = (
        db.query(Appointment)
        .filter(
            Appointment.doctor_id == viewing_doctor.id,
            Appointment.appointment_date == view_date,
        )
        .order_by(Appointment.appointment_time)
        .all()
    )
    for a in appointments:
        a.patient  # lazy-load patient

    return templates.TemplateResponse(request, "appointments.html", {
        "doctor": doctor,
        "viewing_doctor": viewing_doctor,
        "clinic_doctors": clinic_doctors,
        "appointments": appointments,
        "view_date": view_date,
        "today": today,
        "prev_date": (view_date - timedelta(days=1)).isoformat(),
        "next_date": (view_date + timedelta(days=1)).isoformat(),
        "active": "appointments",
    })


# ------------------------------------------------------------------ #
#  Available Slots — JSON (for AJAX on new-appointment form)           #
# ------------------------------------------------------------------ #

@router.get("/slots")
def available_slots(
    date_str: str = Query(..., alias="date"),
    for_doctor_id: int = Query(default=0),
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    target = _resolve_target_doctor(for_doctor_id, doctor, db)
    try:
        appt_date = date.fromisoformat(date_str)
    except ValueError:
        return JSONResponse({"slots": [], "error": "Invalid date"})
    slots = get_available_slots(target.id, appt_date, db)
    return JSONResponse({"slots": slots})


# ------------------------------------------------------------------ #
#  New Appointment — GET                                               #
# ------------------------------------------------------------------ #

@router.get("/new", response_class=HTMLResponse)
def new_appointment_page(
    request: Request,
    prefill_date: str = Query(default=""),
    patient_id: int = Query(default=0),
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    today = date.today()
    try:
        initial_date = date.fromisoformat(prefill_date) if prefill_date else today
    except ValueError:
        initial_date = today

    clinic_doctors = _get_owner_clinic_doctors(doctor, db)

    # Pre-fill from patient if patient_id provided
    form_data = {}
    prefill_doctor_id = doctor.id
    if patient_id:
        patient = db.query(Patient).filter(
            Patient.id == patient_id,
            Patient.doctor_id == doctor.id,
        ).first()
        if patient:
            form_data["patient_name"]  = patient.name
            form_data["patient_phone"] = patient.phone
            # Find the last appointment's doctor for this patient
            last_appt = (
                db.query(Appointment)
                .filter(Appointment.patient_id == patient.id)
                .order_by(Appointment.appointment_date.desc(), Appointment.appointment_time.desc())
                .first()
            )
            if last_appt:
                prefill_doctor_id = last_appt.doctor_id
                form_data["for_doctor_id"] = last_appt.doctor_id

    target_id = form_data.get("for_doctor_id", doctor.id)
    slots = get_available_slots(target_id, initial_date, db)

    return templates.TemplateResponse(request, "appointment_new.html", {
        "doctor": doctor,
        "clinic_doctors": clinic_doctors,
        "today": today.isoformat(),
        "initial_date": initial_date.isoformat(),
        "slots": slots,
        "appointment_types": [e.value for e in AppointmentType if e != AppointmentType.emergency],
        "active": "appointments",
        "error": None,
        "form_data": form_data,
    })


# ------------------------------------------------------------------ #
#  Create Appointment — POST                                           #
# ------------------------------------------------------------------ #

@router.post("", response_class=HTMLResponse)
async def create_appointment(
    request: Request,
    patient_name: str = Form(...),
    patient_phone: str = Form(...),
    appt_date: str = Form(...),
    appt_time: str = Form(...),
    appointment_type: str = Form("follow_up"),
    duration: int = Form(15),
    patient_notes: str = Form(""),
    booked_by_field: str = Form("doctor"),
    for_doctor_id: int = Form(0),
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    target = _resolve_target_doctor(for_doctor_id, doctor, db)
    today = date.today()
    clinic_doctors = _get_owner_clinic_doctors(doctor, db)
    form_data = {
        "patient_name": patient_name,
        "patient_phone": patient_phone,
        "appt_date": appt_date,
        "appt_time": appt_time,
        "appointment_type": appointment_type,
        "duration": duration,
        "patient_notes": patient_notes,
        "for_doctor_id": for_doctor_id,
    }

    def render_error(msg: str):
        try:
            d = date.fromisoformat(appt_date)
        except (ValueError, TypeError):
            d = today
        slots = get_available_slots(target.id, d, db)
        return templates.TemplateResponse(request, "appointment_new.html", {
            "doctor": doctor,
            "clinic_doctors": clinic_doctors,
            "today": today.isoformat(),
            "initial_date": appt_date,
            "slots": slots,
            "appointment_types": [e.value for e in AppointmentType if e != AppointmentType.emergency],
            "active": "appointments",
            "error": msg,
            "form_data": form_data,
        })

    # Parse date / time
    try:
        appt_date_obj = date.fromisoformat(appt_date)
        appt_time_obj = time.fromisoformat(appt_time)
    except ValueError:
        return render_error("Invalid date or time. Please pick a valid slot.")

    # Validate patient fields
    name = patient_name.strip()
    phone = patient_phone.strip()
    if not name:
        return render_error("Patient name is required.")
    if not phone or len(phone) < 10:
        return render_error("A valid phone number is required (at least 10 digits).")

    # Duplicate open appointment check
    if has_open_appointment_on_date(target.id, phone, appt_date_obj, db):
        return render_error(
            "This patient already has a scheduled appointment on this day. "
            "Mark it as completed, no-show, or cancelled before booking again."
        )

    # Slot availability check
    ok, reason = is_slot_available(target.id, appt_date_obj, appt_time_obj, db)
    if not ok:
        return render_error(reason)

    # Get or create patient
    patient = get_or_create_patient(target.id, name, phone, db)

    # Parse appointment type
    try:
        appt_type = AppointmentType(appointment_type)
    except ValueError:
        appt_type = AppointmentType.follow_up

    # Map booked_by field — only accept valid logged-in booking channels
    booked_by_map = {"doctor": BookedBy.doctor, "staff_shared": BookedBy.staff_shared}
    booked_by_val = booked_by_map.get(booked_by_field, BookedBy.doctor)

    # Create the appointment
    appt = Appointment(
        doctor_id=target.id,
        patient_id=patient.id,
        appointment_date=appt_date_obj,
        appointment_time=appt_time_obj,
        duration_mins=duration,
        appointment_type=appt_type,
        patient_notes=patient_notes.strip() or None,
        booked_by=booked_by_val,
        status=AppointmentStatus.scheduled,
    )
    db.add(appt)

    # Update patient visit stats
    if patient.first_visit is None:
        patient.first_visit = appt_date_obj
    patient.last_visit = appt_date_obj
    patient.visit_count = (patient.visit_count or 0) + 1

    db.commit()
    db.refresh(appt)

    # Send WhatsApp confirmation (non-blocking — failure won't break booking)
    try:
        notify_appointment_confirmed(appt, doctor, db)
    except Exception:
        pass

    return RedirectResponse(url=f"/appointments/{appt.id}", status_code=303)


# ------------------------------------------------------------------ #
#  Walk-in Quick Create — POST                                         #
# ------------------------------------------------------------------ #

@router.post("/walkin", response_class=HTMLResponse)
async def create_walkin(
    request: Request,
    patient_name: str = Form(...),
    patient_phone: str = Form(...),
    patient_notes: str = Form(""),
    for_doctor_id: int = Form(0),
    is_emergency: str = Form(""),   # "on" if emergency checkbox ticked
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    target = _resolve_target_doctor(for_doctor_id, doctor, db)
    name  = patient_name.strip()
    phone = patient_phone.strip()
    emergency = is_emergency == "on"

    # Validate inputs
    if not name or not phone or len(phone) < 10:
        today = date.today()
        return RedirectResponse(
            url=f"/appointments?filter_date={today.isoformat()}&walkin_error=1",
            status_code=303,
        )

    patient = get_or_create_patient(target.id, name, phone, db)

    now = datetime.now()
    appt_date = now.date()
    appt_time = now.time().replace(second=0, microsecond=0)

    # Emergencies bypass all slot/quota/hours checks.
    # Regular walk-ins: still admitted (they consume the walk_in_buffer).
    appt = Appointment(
        doctor_id=target.id,
        patient_id=patient.id,
        appointment_date=appt_date,
        appointment_time=appt_time,
        duration_mins=15,
        appointment_type=AppointmentType.emergency if emergency else AppointmentType.new_patient,
        patient_notes=patient_notes.strip() or None,
        booked_by=BookedBy.walk_in,
        is_emergency=emergency,
        status=AppointmentStatus.scheduled,
    )
    db.add(appt)

    if patient.first_visit is None:
        patient.first_visit = appt_date
    patient.last_visit  = appt_date
    patient.visit_count = (patient.visit_count or 0) + 1

    db.commit()
    db.refresh(appt)

    # Walk-ins / emergencies skip WhatsApp — patient is on-site
    return RedirectResponse(url=f"/appointments/{appt.id}", status_code=303)


# ------------------------------------------------------------------ #
#  Detail — GET                                                        #
# ------------------------------------------------------------------ #

@router.get("/{appt_id}", response_class=HTMLResponse)
def appointment_detail(
    appt_id: int,
    request: Request,
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    appt = db.query(Appointment).filter(
        Appointment.id == appt_id,
        Appointment.doctor_id == doctor.id,
    ).first()
    if not appt:
        return RedirectResponse(url="/appointments", status_code=303)

    appt.patient  # lazy-load

    return templates.TemplateResponse(request, "appointment_detail.html", {
        "doctor": doctor,
        "appt": appt,
        "active": "appointments",
        "AppointmentStatus": AppointmentStatus,
    })


# ------------------------------------------------------------------ #
#  Update Status — POST                                                #
# ------------------------------------------------------------------ #

@router.post("/{appt_id}/status", response_class=HTMLResponse)
def update_status(
    appt_id: int,
    status: str = Form(...),
    doctor_notes: str = Form(""),
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    appt = db.query(Appointment).filter(
        Appointment.id == appt_id,
        Appointment.doctor_id == doctor.id,
    ).first()
    if not appt:
        return RedirectResponse(url="/appointments", status_code=303)

    try:
        appt.status = AppointmentStatus(status)
    except ValueError:
        pass

    if doctor_notes.strip():
        appt.doctor_notes = doctor_notes.strip()

    db.commit()
    return RedirectResponse(url=f"/appointments/{appt_id}", status_code=303)


# ------------------------------------------------------------------ #
#  Edit Appointment — GET                                              #
# ------------------------------------------------------------------ #

@router.get("/{appt_id}/edit", response_class=HTMLResponse)
def edit_appointment_page(
    appt_id: int,
    request: Request,
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    appt = db.query(Appointment).filter(
        Appointment.id == appt_id,
        Appointment.doctor_id == doctor.id,
    ).first()
    if not appt:
        return RedirectResponse(url="/appointments", status_code=303)

    appt.patient  # lazy-load
    slots = get_available_slots(doctor.id, appt.appointment_date, db)

    # Always include the current time in the slots list so it shows as selected
    current_time_str = appt.appointment_time.strftime("%H:%M")
    if current_time_str not in slots:
        slots = [current_time_str] + slots

    return templates.TemplateResponse(request, "appointment_edit.html", {
        "doctor": doctor,
        "appt": appt,
        "today": date.today().isoformat(),
        "initial_date": appt.appointment_date.isoformat(),
        "slots": slots,
        "appointment_types": [e.value for e in AppointmentType if e != AppointmentType.emergency],
        "active": "appointments",
        "error": None,
    })


# ------------------------------------------------------------------ #
#  Edit Appointment — POST                                             #
# ------------------------------------------------------------------ #

@router.post("/{appt_id}/edit", response_class=HTMLResponse)
async def edit_appointment(
    appt_id: int,
    request: Request,
    patient_name: str = Form(""),
    patient_phone: str = Form(""),
    appt_date: str = Form(...),
    appt_time: str = Form(...),
    appointment_type: str = Form("follow_up"),
    duration: int = Form(15),
    patient_notes: str = Form(""),
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    appt = db.query(Appointment).filter(
        Appointment.id == appt_id,
        Appointment.doctor_id == doctor.id,
    ).first()
    if not appt:
        return RedirectResponse(url="/appointments", status_code=303)

    appt.patient  # lazy-load
    today = date.today()

    def render_error(msg: str):
        try:
            d = date.fromisoformat(appt_date)
        except (ValueError, TypeError):
            d = appt.appointment_date
        slots = get_available_slots(doctor.id, d, db)
        current_str = appt.appointment_time.strftime("%H:%M")
        if current_str not in slots:
            slots = [current_str] + slots
        return templates.TemplateResponse(request, "appointment_edit.html", {
            "doctor": doctor,
            "appt": appt,
            "today": today.isoformat(),
            "initial_date": appt_date,
            "slots": slots,
            "appointment_types": [e.value for e in AppointmentType if e != AppointmentType.emergency],
            "active": "appointments",
            "error": msg,
        })

    try:
        appt_date_obj = date.fromisoformat(appt_date)
        appt_time_obj = time.fromisoformat(appt_time)
    except ValueError:
        return render_error("Invalid date or time. Please select a valid slot.")

    # Only validate slot if date or time actually changed
    date_changed = appt_date_obj != appt.appointment_date
    time_changed = appt_time_obj != appt.appointment_time

    if date_changed or time_changed:
        ok, reason = is_slot_available_for_edit(
            doctor.id, appt_date_obj, appt_time_obj, appt_id, db
        )
        if not ok:
            return render_error(reason)

    try:
        appt_type = AppointmentType(appointment_type)
    except ValueError:
        appt_type = appt.appointment_type

    appt.appointment_date = appt_date_obj
    appt.appointment_time = appt_time_obj
    appt.appointment_type = appt_type
    appt.duration_mins    = duration
    appt.patient_notes    = patient_notes.strip() or None

    # Update patient name / phone if changed
    if appt.patient:
        if patient_name.strip():
            appt.patient.name = patient_name.strip()
        if patient_phone.strip():
            appt.patient.phone = patient_phone.strip()

    db.commit()
    return RedirectResponse(url=f"/appointments/{appt_id}", status_code=303)
