import calendar as cal_module
from datetime import date, datetime, time as dtime
from fastapi import APIRouter, Request, Depends, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import Optional, List

from database.connection import get_db
from database.models import (
    Doctor, Appointment, Patient, AppointmentStatus,
    DoctorSchedule, BlockedDate,
)
from services.auth_service import get_current_doctor

router = APIRouter(tags=["doctors"])
templates = Jinja2Templates(directory="templates")

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# ------------------------------------------------------------------ #
#  Dashboard                                                           #
# ------------------------------------------------------------------ #

@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    doctor: Doctor = Depends(get_current_doctor),
    db: Session = Depends(get_db),
):
    today = date.today()

    todays_appointments = (
        db.query(Appointment)
        .filter(
            Appointment.doctor_id == doctor.id,
            Appointment.appointment_date == today,
            Appointment.status != AppointmentStatus.cancelled,
        )
        .order_by(Appointment.appointment_time)
        .all()
    )

    for appt in todays_appointments:
        appt.patient  # lazy-load

    total_patients = db.query(func.count(Patient.id)).filter(Patient.doctor_id == doctor.id).scalar()
    total_today = len(todays_appointments)
    completed_today = sum(1 for a in todays_appointments if a.status == AppointmentStatus.completed)
    pending_today = sum(1 for a in todays_appointments if a.status == AppointmentStatus.scheduled)

    now_time = datetime.now().time()
    next_appointment = next(
        (a for a in todays_appointments
         if a.status == AppointmentStatus.scheduled and a.appointment_time >= now_time),
        None,
    )

    trial_active = False
    days_left = None
    if doctor.plan_type.value == "trial" and doctor.trial_ends_at:
        delta = (doctor.trial_ends_at.date() - today).days
        trial_active = delta >= 0
        days_left = max(delta, 0)

    return templates.TemplateResponse(request, "dashboard.html", {
        "doctor": doctor,
        "today": today,
        "todays_appointments": todays_appointments,
        "total_patients": total_patients,
        "total_today": total_today,
        "completed_today": completed_today,
        "pending_today": pending_today,
        "next_appointment": next_appointment,
        "trial_active": trial_active,
        "days_left": days_left,
        "active": "dashboard",
    })


# ------------------------------------------------------------------ #
#  Settings — GET                                                      #
# ------------------------------------------------------------------ #

@router.get("/doctors/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    doctor: Doctor = Depends(get_current_doctor),
    db: Session = Depends(get_db),
    saved: str = "",
):
    schedules_db = db.query(DoctorSchedule).filter(DoctorSchedule.doctor_id == doctor.id).all()
    schedule_map = {s.day_of_week: s for s in schedules_db}

    # Build a list of 7 day dicts for the template
    days_data = []
    for i, name in enumerate(DAYS):
        s = schedule_map.get(i)
        days_data.append({
            "index": i,
            "name": name,
            "is_active": s.is_active if s else False,
            "start_time": s.start_time.strftime("%H:%M") if s else "09:00",
            "end_time": s.end_time.strftime("%H:%M") if s else "18:00",
            "slot_duration": s.slot_duration if s else 15,
            "max_patients": s.max_patients if s else 30,
        })

    blocked = (
        db.query(BlockedDate)
        .filter(BlockedDate.doctor_id == doctor.id)
        .order_by(BlockedDate.blocked_date)
        .all()
    )

    return templates.TemplateResponse(request, "settings.html", {
        "doctor": doctor,
        "days_data": days_data,
        "blocked_dates": blocked,
        "saved": saved == "1",
        "active": "settings",
    })


# ------------------------------------------------------------------ #
#  Settings — Save Schedule                                            #
# ------------------------------------------------------------------ #

@router.post("/doctors/settings/schedule", response_class=HTMLResponse)
async def save_schedule(
    request: Request,
    doctor: Doctor = Depends(get_current_doctor),
    db: Session = Depends(get_db),
):
    form = await request.form()

    for i in range(7):
        is_active = form.get(f"active_{i}") == "on"
        start_str = form.get(f"start_{i}", "09:00")
        end_str = form.get(f"end_{i}", "18:00")
        slot_dur = int(form.get(f"slot_{i}", 15))
        max_pat = int(form.get(f"max_{i}", 30))

        try:
            start_t = dtime.fromisoformat(start_str)
            end_t = dtime.fromisoformat(end_str)
        except ValueError:
            continue

        existing = db.query(DoctorSchedule).filter(
            DoctorSchedule.doctor_id == doctor.id,
            DoctorSchedule.day_of_week == i,
        ).first()

        if existing:
            existing.is_active = is_active
            existing.start_time = start_t
            existing.end_time = end_t
            existing.slot_duration = slot_dur
            existing.max_patients = max_pat
        else:
            db.add(DoctorSchedule(
                doctor_id=doctor.id,
                day_of_week=i,
                start_time=start_t,
                end_time=end_t,
                slot_duration=slot_dur,
                max_patients=max_pat,
                is_active=is_active,
            ))

    db.commit()
    return RedirectResponse(url="/doctors/settings?saved=1", status_code=303)


# ------------------------------------------------------------------ #
#  Settings — Save Profile                                             #
# ------------------------------------------------------------------ #

@router.post("/doctors/settings/profile", response_class=HTMLResponse)
def save_profile(
    request: Request,
    clinic_name: str = Form(""),
    city: str = Form(""),
    specialization: str = Form(""),
    clinic_address: str = Form(""),
    languages: str = Form(""),
    doctor: Doctor = Depends(get_current_doctor),
    db: Session = Depends(get_db),
):
    doctor.clinic_name = clinic_name.strip() or None
    doctor.city = city.strip() or None
    doctor.specialization = specialization.strip() or None
    doctor.clinic_address = clinic_address.strip() or None
    doctor.languages = languages.strip() or None
    db.commit()
    return RedirectResponse(url="/doctors/settings?saved=1", status_code=303)


# ------------------------------------------------------------------ #
#  Settings — Add Blocked Date                                         #
# ------------------------------------------------------------------ #

@router.post("/doctors/settings/block", response_class=HTMLResponse)
def add_blocked_date(
    request: Request,
    blocked_date: str = Form(...),
    reason: str = Form(""),
    doctor: Doctor = Depends(get_current_doctor),
    db: Session = Depends(get_db),
):
    try:
        d = date.fromisoformat(blocked_date)
    except ValueError:
        return RedirectResponse(url="/doctors/settings", status_code=303)

    exists = db.query(BlockedDate).filter(
        BlockedDate.doctor_id == doctor.id,
        BlockedDate.blocked_date == d,
    ).first()

    if not exists:
        db.add(BlockedDate(doctor_id=doctor.id, blocked_date=d, reason=reason.strip() or None))
        db.commit()

    return RedirectResponse(url="/doctors/settings?saved=1", status_code=303)


# ------------------------------------------------------------------ #
#  Settings — Remove Blocked Date                                      #
# ------------------------------------------------------------------ #

@router.post("/doctors/settings/unblock/{block_id}", response_class=HTMLResponse)
def remove_blocked_date(
    block_id: int,
    doctor: Doctor = Depends(get_current_doctor),
    db: Session = Depends(get_db),
):
    record = db.query(BlockedDate).filter(
        BlockedDate.id == block_id,
        BlockedDate.doctor_id == doctor.id,  # security: own records only
    ).first()
    if record:
        db.delete(record)
        db.commit()
    return RedirectResponse(url="/doctors/settings", status_code=303)


# ------------------------------------------------------------------ #
#  Calendar                                                            #
# ------------------------------------------------------------------ #

@router.get("/calendar", response_class=HTMLResponse)
def calendar_view(
    request: Request,
    month: str = Query(default=""),
    doctor: Doctor = Depends(get_current_doctor),
    db: Session = Depends(get_db),
):
    today = date.today()

    # Parse ?month=YYYY-MM, fall back to current month
    try:
        year, mon = map(int, month.split("-")) if month else (today.year, today.month)
        if not (1 <= mon <= 12):
            raise ValueError
    except (ValueError, AttributeError):
        year, mon = today.year, today.month

    first_day = date(year, mon, 1)
    last_day  = date(year, mon, cal_module.monthrange(year, mon)[1])

    # Prev / next month strings
    if mon == 1:
        prev_month = f"{year - 1}-12"
    else:
        prev_month = f"{year}-{mon - 1:02d}"
    if mon == 12:
        next_month = f"{year + 1}-01"
    else:
        next_month = f"{year}-{mon + 1:02d}"

    # Appointments for this month (non-cancelled)
    month_appts = (
        db.query(Appointment)
        .filter(
            Appointment.doctor_id == doctor.id,
            Appointment.appointment_date >= first_day,
            Appointment.appointment_date <= last_day,
            Appointment.status != AppointmentStatus.cancelled,
        )
        .all()
    )

    # Group by ISO date string
    appt_by_date: dict = {}
    for a in month_appts:
        key = a.appointment_date.isoformat()
        appt_by_date.setdefault(key, []).append(a)

    # Blocked dates for this month
    blocked = db.query(BlockedDate).filter(
        BlockedDate.doctor_id == doctor.id,
        BlockedDate.blocked_date >= first_day,
        BlockedDate.blocked_date <= last_day,
    ).all()
    blocked_set = {b.blocked_date.isoformat() for b in blocked}

    # Build cal_data: list of weeks → list of day-dicts (None = padding cell)
    cal_data = []
    for week in cal_module.monthcalendar(year, mon):
        week_data = []
        for day_num in week:
            if day_num == 0:
                week_data.append(None)
            else:
                d   = date(year, mon, day_num)
                key = d.isoformat()
                day_appts = appt_by_date.get(key, [])
                week_data.append({
                    "num":       day_num,
                    "date_str":  key,
                    "total":     len(day_appts),
                    "scheduled": sum(1 for a in day_appts if a.status == AppointmentStatus.scheduled),
                    "completed": sum(1 for a in day_appts if a.status == AppointmentStatus.completed),
                    "no_show":   sum(1 for a in day_appts if a.status == AppointmentStatus.no_show),
                    "is_today":  d == today,
                    "is_blocked": key in blocked_set,
                    "is_past":   d < today,
                })
        cal_data.append(week_data)

    current_month = f"{today.year}-{today.month:02d}"
    viewing_current = (year == today.year and mon == today.month)

    return templates.TemplateResponse(request, "calendar.html", {
        "doctor":          doctor,
        "today":           today,
        "year":            year,
        "mon":             mon,
        "month_name":      first_day.strftime("%B %Y"),
        "cal_data":        cal_data,
        "prev_month":      prev_month,
        "next_month":      next_month,
        "current_month":   current_month,
        "viewing_current": viewing_current,
        "active":          "calendar",
    })
