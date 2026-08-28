from datetime import date, time, timedelta, datetime
from fastapi import APIRouter, Request, Depends, Form, Query, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import case as sa_case
from sqlalchemy.orm import Session, joinedload

from database.connection import get_db
from database.models import (
    Doctor, Patient, Appointment, AppointmentStatus, AppointmentType, BookedBy,
    ClinicDoctor, Clinic, Visit, VisitStatus, Bill, PriceCatalog, DoctorSchedule,
    Prescription,
)
from services.auth_service import get_paying_doctor, get_appt_doctor
from services.appointment_service import (
    get_available_slots, is_slot_available, is_slot_available_for_edit,
    get_or_create_patient, has_open_appointment_on_date,
)
import services.visit_service as vs

router = APIRouter(prefix="/appointments", tags=["appointments"])
templates = Jinja2Templates(directory="templates")


def _active_clinic_id(request) -> int | None:
    """The clinic this request is operating in, stashed by get_paying_doctor."""
    return getattr(request.state, "active_clinic_id", None)


def _get_owner_clinic_doctors(doctor: Doctor, db: Session, request=None) -> list[Doctor]:
    """Doctors the caller may act for — only inside the clinic they OWN.

    Bound to the active clinic rather than an arbitrary owned one: a doctor
    who owns clinic A and is an associate at clinic B must not get a doctor
    selector while working at B.
    """
    from services.clinic_context import is_owner_of, active_memberships, ROLE_OWNER
    from database.models import ClinicDoctor

    clinic_id = _active_clinic_id(request) if request is not None else None
    if clinic_id is None:
        owned = next((m for m in active_memberships(db, doctor.id)
                      if m.role == ROLE_OWNER), None)
        if not owned:
            return []
        clinic_id = owned.clinic_id
    elif not is_owner_of(db, doctor.id, clinic_id):
        return []

    members = db.query(ClinicDoctor).filter(
        ClinicDoctor.clinic_id == clinic_id, ClinicDoctor.is_active == True
    ).all()
    if len(members) < 2:
        return []  # solo clinic, no selector needed
    ids = [m.doctor_id for m in members]
    return db.query(Doctor).filter(Doctor.id.in_(ids)).order_by(Doctor.name).all()


def _resolve_target_doctor(for_doctor_id: int, logged_in_doctor: Doctor, db: Session,
                           request=None) -> Doctor:
    """Act as another doctor — only if you own the clinic you're currently in.

    Previously took any owned-clinic membership regardless of which clinic the
    request concerned, so an owner of clinic A could act as a doctor of A while
    nominally working at clinic B.
    """
    from services.clinic_context import is_owner_of, active_memberships, ROLE_OWNER
    from database.models import ClinicDoctor

    if not for_doctor_id or for_doctor_id == logged_in_doctor.id:
        return logged_in_doctor

    clinic_id = _active_clinic_id(request) if request is not None else None
    if clinic_id is None:
        owned = next((m for m in active_memberships(db, logged_in_doctor.id)
                      if m.role == ROLE_OWNER), None)
        if not owned:
            return logged_in_doctor
        clinic_id = owned.clinic_id
    elif not is_owner_of(db, logged_in_doctor.id, clinic_id):
        return logged_in_doctor

    member = db.query(ClinicDoctor).filter(
        ClinicDoctor.clinic_id == clinic_id,
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
    q: str = Query(default=""),
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

    # Done statuses sink to the bottom; within each group newest-created first
    done_last = sa_case(
        (Appointment.status == AppointmentStatus.completed, 1),
        (Appointment.status == AppointmentStatus.no_show, 1),
        (Appointment.status == AppointmentStatus.cancelled, 1),
        else_=0,
    )

    appt_query = (
        db.query(Appointment)
        .join(Appointment.patient)
        .options(joinedload(Appointment.patient))   # eager-load — avoids N+1
        .filter(
            Appointment.doctor_id == viewing_doctor.id,
            Appointment.appointment_date == view_date,
        )
    )
    if q.strip():
        appt_query = appt_query.filter(
            Patient.name.ilike(f"%{q.strip()}%")
        )

    appointments = (
        appt_query
        .order_by(done_last, Appointment.appointment_time.asc(), Appointment.created_at.desc())
        .all()
    )

    # ── Walk-in availability — any time today (walk-ins are ad-hoc, not slot-bound) ───
    walkin_available = False
    if view_date == today:
        _dow = today.weekday()
        _scheds = db.query(DoctorSchedule).filter(
            DoctorSchedule.doctor_id == viewing_doctor.id,
            DoctorSchedule.day_of_week == _dow,
            DoctorSchedule.is_active == True,
        ).all()
        walkin_available = bool(_scheds)

    # ── Queue data (today only) ───────────────────────────────────────
    visit_map = {}   # appt_id → Visit (for status badges on schedule rows)
    serving = None
    waiting = []
    billing_pending = []
    held = []           # ON HOLD patients (sent for x-ray/lab, resumable)
    flow_stats = None   # Today's Flow widget data

    if view_date == today:
        day_visits = (
            db.query(Visit)
            .options(joinedload(Visit.patient))   # eager-load — avoids N+1
            .filter(Visit.doctor_id == viewing_doctor.id, Visit.visit_date == today)
            .order_by(Visit.is_emergency.desc(), Visit.queue_position.asc())
            .all()
        )
        for v in day_visits:
            if v.appointment_id:
                visit_map[v.appointment_id] = v
            if v.status == VisitStatus.serving:
                serving = v
            elif v.status == VisitStatus.waiting:
                waiting.append(v)
            elif v.status == VisitStatus.on_hold:
                held.append(v)
            elif v.status == VisitStatus.billing_pending:
                billing_pending.append(v)

        # ── Today's Flow stats ───────────────────────────────────────
        from datetime import datetime as _dt
        _done_v   = [v for v in day_visits if v.status.value == "done"]
        _served_v = [v for v in day_visits if v.status.value in ("done", "billing_pending", "serving")]

        # Avg wait time (check_in → call)
        wait_mins = []
        for v in day_visits:
            if v.check_in_time and v.call_time:
                delta = (v.call_time - v.check_in_time).total_seconds() / 60
                if 0 <= delta <= 300:   # sanity: ignore absurd values
                    wait_mins.append(delta)
        avg_wait = round(sum(wait_mins) / len(wait_mins)) if wait_mins else None

        # Avg consult time (call → complete)
        consult_mins = []
        for v in _done_v:
            if v.call_time and v.complete_time:
                delta = (v.complete_time - v.call_time).total_seconds() / 60
                if 0 <= delta <= 300:
                    consult_mins.append(delta)
        avg_consult = round(sum(consult_mins) / len(consult_mins)) if consult_mins else None

        # On-time % — appt visits called within 20 min of scheduled time
        appt_visits = [v for v in _served_v if v.source.value == "appointment" and v.appointment_id]
        on_time_count = 0
        on_time_total = 0
        for v in appt_visits:
            appt_obj = v.appointment  # use the ORM relationship → returns Appointment, not Visit
            if appt_obj and v.call_time:
                sched_dt = _dt.combine(today, appt_obj.appointment_time)
                late_mins = (v.call_time - sched_dt).total_seconds() / 60
                on_time_total += 1
                if late_mins <= 20:
                    on_time_count += 1
        on_time_pct = round((on_time_count / on_time_total) * 100) if on_time_total else None

        waiting_count = len(waiting)
        serving_count = 1 if serving else 0
        billing_count = len(billing_pending)
        done_count    = len(_done_v)
        total_flow    = waiting_count + serving_count + billing_count + done_count

        flow_stats = {
            "waiting":      waiting_count,
            "serving":      serving_count,
            "billing":      billing_count,
            "done":         done_count,
            "total":        total_flow,
            "avg_wait":     avg_wait,
            "avg_consult":  avg_consult,
            "on_time_pct":  on_time_pct,
        }

    return templates.TemplateResponse(request, "appointments.html", {
        "doctor": doctor,
        "viewing_doctor": viewing_doctor,
        "clinic_doctors": clinic_doctors,
        "appointments": appointments,
        "view_date": view_date,
        "today": today,
        "prev_date": (view_date - timedelta(days=1)).isoformat(),
        "next_date": (view_date + timedelta(days=1)).isoformat(),
        "q": q,
        "active": "appointments",
        # queue
        "visit_map":        visit_map,
        "serving":          serving,
        "waiting":          waiting,
        "held":             held,
        "billing_pending":  billing_pending,
        "is_today":         view_date == today,
        "walkin_available": walkin_available,
        "flow_stats":       flow_stats,
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
    slots = get_available_slots(target.id, appt_date, db, filter_past=True)
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
    slots = get_available_slots(target_id, initial_date, db, filter_past=True)

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
    background_tasks: BackgroundTasks,
    patient_name: str = Form(...),
    patient_phone: str = Form(...),
    patient_age: str = Form(""),
    patient_gender: str = Form(""),
    appt_date: str = Form(...),
    appt_time: str = Form(...),
    appointment_type: str = Form("new_patient"),
    duration: int = Form(15),
    patient_notes: str = Form(""),
    booked_by_field: str = Form("staff_shared"),
    for_doctor_id: int = Form(0),
    referral_source: str = Form(""),
    referral_source_other: str = Form(""),
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    target = _resolve_target_doctor(for_doctor_id, doctor, db)
    today = date.today()
    clinic_doctors = _get_owner_clinic_doctors(doctor, db)
    form_data = {
        "patient_name": patient_name,
        "patient_phone": patient_phone,
        "patient_age": patient_age,
        "patient_gender": patient_gender,
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
        slots = get_available_slots(target.id, d, db, filter_past=True)
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
    if not phone or not phone.isdigit() or len(phone) != 10:
        return render_error("Phone number must be exactly 10 digits.")

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

    # Parse optional age / gender
    age_val = int(patient_age) if patient_age.strip().isdigit() else None
    gender_val = patient_gender.strip() or None

    # Get or create patient (with first-touch source attribution)
    patient = get_or_create_patient(
        target.id, name, phone, db,
        age=age_val, gender=gender_val,
        referral_source=(referral_source.strip() or None),
        referral_source_other=(referral_source_other.strip() or None),
    )

    # Parse appointment type
    try:
        appt_type = AppointmentType(appointment_type)
    except ValueError:
        appt_type = AppointmentType.new_patient

    # Map booked_by field — only accept valid logged-in booking channels
    booked_by_map = {"doctor": BookedBy.doctor, "staff_shared": BookedBy.staff_shared}
    booked_by_val = booked_by_map.get(booked_by_field, BookedBy.staff_shared)

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

    # Send WhatsApp confirmation AFTER the response (never blocks the booking)
    from services.notification_service import send_appointment_confirmed_bg, send_followup_confirmed_bg
    from database.models import AppointmentType as _AppointmentType
    if appt.appointment_type == _AppointmentType.follow_up:
        background_tasks.add_task(send_followup_confirmed_bg, appt.id, doctor.id)
    else:
        background_tasks.add_task(send_appointment_confirmed_bg, appt.id, doctor.id)

    return RedirectResponse(url=f"/appointments?filter_date={appt.appointment_date.isoformat()}", status_code=303)


# ------------------------------------------------------------------ #
#  Patient phone lookup — JSON (walk-in autofill)                      #
# ------------------------------------------------------------------ #

@router.get("/patient-lookup")
def patient_phone_lookup(
    phone: str = Query(...),
    for_doctor_id: int = Query(default=0),
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    target = _resolve_target_doctor(for_doctor_id, doctor, db)
    phone = phone.strip()
    if not phone.isdigit() or len(phone) != 10:
        return JSONResponse({"found": False})
    patient = db.query(Patient).filter(
        Patient.doctor_id == target.id,
        Patient.phone == phone,
    ).first()
    if not patient:
        return JSONResponse({"found": False})
    return JSONResponse({
        "found": True,
        "name": patient.name,
        "age": patient.age,
        "gender": patient.gender or "",
    })


# ------------------------------------------------------------------ #
#  Walk-in Quick Create — POST                                         #
# ------------------------------------------------------------------ #

@router.post("/walkin", response_class=HTMLResponse)
async def create_walkin(
    request: Request,
    background_tasks: BackgroundTasks,
    patient_name: str = Form(...),
    patient_phone: str = Form(...),
    patient_age: str = Form(""),
    patient_gender: str = Form(""),
    patient_notes: str = Form(""),
    for_doctor_id: int = Form(0),
    is_emergency: str = Form(""),   # "on" if emergency checkbox ticked
    referral_source: str = Form(""),
    referral_source_other: str = Form(""),
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    target = _resolve_target_doctor(for_doctor_id, doctor, db)
    name  = patient_name.strip()
    phone = patient_phone.strip()
    emergency = is_emergency == "on"

    # Validate inputs
    if not name or not phone or not phone.isdigit() or len(phone) != 10:
        today = date.today()
        return RedirectResponse(
            url=f"/appointments?filter_date={today.isoformat()}&walkin_error=1",
            status_code=303,
        )

    age_val    = int(patient_age) if patient_age.strip().isdigit() else None
    gender_val = patient_gender.strip() or None
    patient = get_or_create_patient(
        target.id, name, phone, db,
        age=age_val, gender=gender_val,
        referral_source=(referral_source.strip() or None),
        referral_source_other=(referral_source_other.strip() or None),
    )

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

    # Auto-check-in walk-in to the live queue immediately
    membership = db.query(ClinicDoctor).filter(
        ClinicDoctor.doctor_id == target.id,
        ClinicDoctor.is_active == True,
    ).first()
    clinic_id = membership.clinic_id if membership else None

    vs.check_in(
        db,
        doctor_id      = target.id,
        patient_id     = patient.id,
        clinic_id      = clinic_id,
        appointment_id = appt.id,
        is_emergency   = emergency,
        created_by     = doctor.id,
    )

    # Notify walk-in patient of queue position AFTER the response (non-blocking)
    from database.models import Visit as VisitModel
    today_visit = db.query(VisitModel).filter(
        VisitModel.doctor_id == target.id,
        VisitModel.patient_id == patient.id,
        VisitModel.visit_date == datetime.now().date(),
    ).order_by(VisitModel.id.desc()).first()
    if today_visit and patient.phone:
        from services.notification_service import send_walkin_queued_bg
        background_tasks.add_task(send_walkin_queued_bg, today_visit.id, target.id)

    return RedirectResponse(url=f"/appointments?filter_date={appt_date.isoformat()}", status_code=303)


# ------------------------------------------------------------------ #
#  Detail — GET (redirects to floating card; page removed)             #
# ------------------------------------------------------------------ #

@router.get("/{appt_id}", response_class=HTMLResponse)
def appointment_detail_redirect(
    appt_id: int,
    request: Request,
    doctor: Doctor = Depends(get_appt_doctor),
    db: Session = Depends(get_db),
):
    """Appointment detail page removed — redirect back to the list."""
    appt = db.query(Appointment).filter(
        Appointment.id == appt_id,
        Appointment.doctor_id == doctor.id,
    ).first()
    back = "/clinic/reception" if getattr(request.state, "is_staff", False) else "/appointments"
    if appt:
        back += f"?filter_date={appt.appointment_date.isoformat()}"
    return RedirectResponse(url=back, status_code=303)


# ------------------------------------------------------------------ #
#  Appointment Card Partial — GET (for floating overlay)               #
# ------------------------------------------------------------------ #

@router.get("/{appt_id}/card", response_class=HTMLResponse)
def appointment_card(
    appt_id: int,
    request: Request,
    doctor: Doctor = Depends(get_appt_doctor),
    db: Session = Depends(get_db),
):
    appt = db.query(Appointment).filter(
        Appointment.id == appt_id,
        Appointment.doctor_id == doctor.id,
    ).first()
    if not appt:
        return HTMLResponse("<div style='padding:40px;text-align:center;color:var(--muted)'>Appointment not found.</div>")

    _ = appt.patient  # eager-load

    visit = db.query(Visit).filter(Visit.appointment_id == appt.id).first()
    bill  = db.query(Bill).filter(Bill.visit_id == visit.id).first() if visit else None
    if bill:
        _ = bill.items  # eager-load bill items

    price_catalog = (
        db.query(PriceCatalog)
        .filter(PriceCatalog.doctor_id == doctor.id, PriceCatalog.is_active == True)
        .order_by(PriceCatalog.sort_order, PriceCatalog.name)
        .all()
    )

    # Prescriptions for this visit (shown in card + "Write Prescription" button)
    visit_prescriptions = []
    if visit:
        visit_prescriptions = (
            db.query(Prescription)
            .filter(
                Prescription.visit_id == visit.id,
                Prescription.doctor_id == doctor.id,
            )
            .order_by(Prescription.created_at.desc())
            .all()
        )

    # Payment status
    if bill and float(bill.total or 0) > 0 and float(bill.paid_amount or 0) == 0:
        payment_status = "dues"
    elif bill and bill.payment_mode and bill.payment_mode.value == "free":
        payment_status = "free"
    elif bill:
        payment_status = "paid"
    else:
        payment_status = "none"

    # Initials for avatar
    words    = (appt.patient.name or "?").split()
    initials = (words[0][0] + (words[-1][0] if len(words) > 1 else "")).upper()

    return templates.TemplateResponse(request, "appointment_card.html", {
        "appt":                appt,
        "patient":             appt.patient,
        "visit":               visit,
        "bill":                bill,
        "price_catalog":       price_catalog,
        "visit_prescriptions": visit_prescriptions,
        "payment_status":      payment_status,
        "initials":            initials,
        "AppointmentStatus":   AppointmentStatus,
        "today":               date.today(),
    })


# ------------------------------------------------------------------ #
#  Save Reception Notes — POST                                         #
# ------------------------------------------------------------------ #

@router.post("/{appt_id}/reception-notes")
async def save_reception_notes(
    appt_id: int,
    request: Request,
    doctor: Doctor = Depends(get_appt_doctor),
    db: Session = Depends(get_db),
):
    form  = await request.form()
    notes = (form.get("reception_notes") or "").strip()
    appt  = db.query(Appointment).filter(
        Appointment.id == appt_id,
        Appointment.doctor_id == doctor.id,
    ).first()
    if appt:
        appt.reception_notes = notes or None
        db.commit()
    return JSONResponse({"ok": True})


# ------------------------------------------------------------------ #
#  Card — Full Edit Save — POST                                        #
# ------------------------------------------------------------------ #

@router.post("/{appt_id}/card-save")
async def card_save(
    appt_id: int,
    request: Request,
    doctor: Doctor = Depends(get_appt_doctor),
    db: Session = Depends(get_db),
):
    form = await request.form()
    appt = db.query(Appointment).filter(
        Appointment.id == appt_id,
        Appointment.doctor_id == doctor.id,
    ).first()
    if not appt:
        return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)

    patient = appt.patient

    # ── Patient fields ─────────────────────────────────────────────
    name = (form.get("patient_name") or "").strip()
    if name:
        patient.name = name
    phone = (form.get("patient_phone") or "").strip()
    if phone:
        patient.phone = phone
    age_raw = (form.get("age") or "").strip()
    patient.age = int(age_raw) if age_raw.isdigit() else None
    patient.gender              = (form.get("gender") or "").strip() or None
    patient.blood_group         = (form.get("blood_group") or "").strip() or None
    patient.allergies           = (form.get("allergies") or "").strip() or None
    patient.preferred_contact   = (form.get("preferred_contact") or "phone").strip()

    # ── Appointment fields ──────────────────────────────────────────
    appt_date_raw = (form.get("appointment_date") or "").strip()
    appt_time_raw = (form.get("appointment_time") or "").strip()
    try:
        if appt_date_raw:
            appt.appointment_date = date.fromisoformat(appt_date_raw)
    except ValueError:
        pass
    try:
        if appt_time_raw:
            appt.appointment_time = time.fromisoformat(appt_time_raw)
    except ValueError:
        pass
    dur_raw = (form.get("duration_mins") or "").strip()
    if dur_raw.isdigit():
        appt.duration_mins = int(dur_raw)
    appt_type_raw = (form.get("appointment_type") or "").strip()
    if appt_type_raw:
        try:
            appt.appointment_type = AppointmentType(appt_type_raw)
        except ValueError:
            pass
    appt.patient_notes  = (form.get("patient_notes") or "").strip() or None
    appt.doctor_notes   = (form.get("doctor_notes") or "").strip() or None
    fu_raw = (form.get("follow_up_date") or "").strip()
    try:
        appt.follow_up_date = date.fromisoformat(fu_raw) if fu_raw else None
    except ValueError:
        pass

    db.commit()
    return JSONResponse({"ok": True})


# ------------------------------------------------------------------ #
#  Save Follow-up Date — POST                                          #
# ------------------------------------------------------------------ #

@router.post("/{appt_id}/follow-up")
async def save_follow_up(
    appt_id: int,
    request: Request,
    doctor: Doctor = Depends(get_appt_doctor),
    db: Session = Depends(get_db),
):
    form = await request.form()
    fu   = (form.get("follow_up_date") or "").strip()
    appt = db.query(Appointment).filter(
        Appointment.id == appt_id,
        Appointment.doctor_id == doctor.id,
    ).first()
    if not appt:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)

    # ── 1. Save follow_up_date on parent appointment ──
    fu_date = None
    if fu:
        try:
            fu_date = date.fromisoformat(fu)
            appt.follow_up_date = fu_date
        except ValueError:
            appt.follow_up_date = None
    else:
        appt.follow_up_date = None

    # ── 2. Auto-book first available slot on that date ──
    new_appt_info = None
    if fu_date:
        slots = get_available_slots(doctor.id, fu_date, db, filter_past=False)
        if slots:
            first_slot = time.fromisoformat(slots[0])
            # Use same duration as the parent appointment (or default 15 min)
            duration = appt.duration_mins or 15
            new_appt = Appointment(
                doctor_id        = doctor.id,
                patient_id       = appt.patient_id,
                appointment_date = fu_date,
                appointment_time = first_slot,
                duration_mins    = duration,
                appointment_type = AppointmentType.follow_up,
                status           = AppointmentStatus.scheduled,
                booked_by        = BookedBy.doctor,
            )
            db.add(new_appt)
            db.flush()
            new_appt_info = {
                "id":   new_appt.id,
                "date": fu_date.strftime("%d %b %Y"),
                "time": datetime.combine(fu_date, first_slot).strftime("%I:%M %p").lstrip("0"),
            }

    db.commit()
    return JSONResponse({"ok": True, "new_appt": new_appt_info})


# ------------------------------------------------------------------ #
#  Update Status — POST                                                #
# ------------------------------------------------------------------ #

@router.post("/{appt_id}/status", response_class=HTMLResponse)
def update_status(
    appt_id: int,
    request: Request,
    status: str = Form(...),
    doctor_notes: str = Form(""),
    doctor: Doctor = Depends(get_appt_doctor),
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
    return RedirectResponse(url=f"/appointments?filter_date={appt.appointment_date.isoformat()}", status_code=303)


# ------------------------------------------------------------------ #
#  Edit Appointment — GET                                              #
# ------------------------------------------------------------------ #

@router.get("/{appt_id}/edit", response_class=HTMLResponse)
def edit_appointment_page(
    appt_id: int,
    request: Request,
    doctor: Doctor = Depends(get_appt_doctor),
    db: Session = Depends(get_db),
):
    appt = db.query(Appointment).filter(
        Appointment.id == appt_id,
        Appointment.doctor_id == doctor.id,
    ).first()
    if not appt:
        back = "/clinic/reception" if getattr(request.state, "is_staff", False) else "/appointments"
        return RedirectResponse(url=back, status_code=303)

    appt.patient  # lazy-load
    slots = get_available_slots(doctor.id, appt.appointment_date, db, filter_past=False)

    # Always include the current time in the slots list so it shows as selected
    current_time_str = appt.appointment_time.strftime("%H:%M")
    if current_time_str not in slots:
        slots = [current_time_str] + slots

    is_staff = getattr(request.state, "is_staff", False)
    return templates.TemplateResponse(request, "appointment_edit.html", {
        "doctor": doctor,
        "appt": appt,
        "today": date.today().isoformat(),
        "initial_date": appt.appointment_date.isoformat(),
        "slots": slots,
        "appointment_types": [e.value for e in AppointmentType if e != AppointmentType.emergency],
        "active": "appointments",
        "error": None,
        "is_staff": is_staff,
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
    doctor: Doctor = Depends(get_appt_doctor),
    db: Session = Depends(get_db),
):
    is_staff = getattr(request.state, "is_staff", False)
    appt = db.query(Appointment).filter(
        Appointment.id == appt_id,
        Appointment.doctor_id == doctor.id,
    ).first()
    if not appt:
        back = "/clinic/reception" if is_staff else "/appointments"
        return RedirectResponse(url=back, status_code=303)

    appt.patient  # lazy-load
    today = date.today()

    def render_error(msg: str):
        try:
            d = date.fromisoformat(appt_date)
        except (ValueError, TypeError):
            d = appt.appointment_date
        slots = get_available_slots(doctor.id, d, db, filter_past=False)
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
            "is_staff": is_staff,
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
    return RedirectResponse(url=f"/appointments?filter_date={appt.appointment_date.isoformat()}", status_code=303)


# ------------------------------------------------------------------ #
#  Delete Appointment — POST                                           #
# ------------------------------------------------------------------ #

@router.post("/{appt_id}/delete", response_class=HTMLResponse)
def delete_appointment(
    appt_id: int,
    request: Request,
    doctor: Doctor = Depends(get_appt_doctor),
    db: Session = Depends(get_db),
):
    is_staff = getattr(request.state, "is_staff", False)
    appt = db.query(Appointment).filter(
        Appointment.id == appt_id,
        Appointment.doctor_id == doctor.id,
    ).first()
    if not appt:
        back = "/clinic/reception" if is_staff else "/appointments"
        return RedirectResponse(url=back, status_code=303)

    appt_date = appt.appointment_date.isoformat()
    db.delete(appt)
    db.commit()

    back = f"/clinic/reception?filter_date={appt_date}" if is_staff else f"/appointments?filter_date={appt_date}"
    return RedirectResponse(url=back, status_code=303)
