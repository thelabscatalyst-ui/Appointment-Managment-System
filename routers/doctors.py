import calendar as cal_module
from datetime import date, datetime, time as dtime
from fastapi import APIRouter, Request, Depends, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func, case
from typing import Optional, List

from database.connection import get_db
from database.models import (
    Doctor, Appointment, Patient, AppointmentStatus, BookedBy,
    DoctorSchedule, BlockedDate, BlockedTime, PriceCatalog,
    Bill, Expense, ExpenseCategory, PaymentMode,
    Visit, VisitStatus, ReferralSource,
)
from config import settings
from services.auth_service import (
    get_current_doctor, get_paying_doctor,
    require_pin, require_pin_auth,
    create_pin_token, decode_pin_token,
    hash_password, verify_password,
)

router = APIRouter(tags=["doctors"])
templates = Jinja2Templates(directory="templates")

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# ------------------------------------------------------------------ #
#  Workspace Loading Screen                                            #
# ------------------------------------------------------------------ #

@router.get("/workspace-loading", response_class=HTMLResponse)
def workspace_loading(
    request: Request,
    doctor: Doctor = Depends(get_current_doctor),
):
    return templates.TemplateResponse(request, "workspace_loading.html", {
        "doctor": doctor,
    })


# ------------------------------------------------------------------ #
#  Dashboard                                                           #
# ------------------------------------------------------------------ #

@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
    upgraded: str = "",
):
    today = date.today()

    all_today = (
        db.query(Appointment)
        .options(joinedload(Appointment.patient))   # eager-load — avoids N+1
        .filter(
            Appointment.doctor_id   == doctor.id,
            Appointment.appointment_date == today,
        )
        .order_by(Appointment.appointment_time)
        .all()
    )

    _done_statuses = {
        AppointmentStatus.completed,
        AppointmentStatus.no_show,
        AppointmentStatus.cancelled,
    }

    # Active = scheduled (show at top)
    todays_appointments = [a for a in all_today if a.status not in _done_statuses]
    # Past = completed / no_show / cancelled (collapsible section)
    past_appointments   = [a for a in all_today if a.status in _done_statuses]

    total_patients   = db.query(func.count(Patient.id)).filter(Patient.doctor_id == doctor.id).scalar()
    total_today      = len(all_today)
    completed_today  = sum(1 for a in all_today if a.status == AppointmentStatus.completed)
    pending_today    = sum(1 for a in all_today if a.status == AppointmentStatus.scheduled)
    walkin_today     = sum(1 for a in all_today if a.booked_by == BookedBy.walk_in and not a.is_emergency)
    emergency_today  = sum(1 for a in all_today if a.is_emergency)

    # Show the earliest still-open appointment regardless of whether its time has passed
    next_appointment = next(
        (a for a in todays_appointments if a.status == AppointmentStatus.scheduled),
        None,
    )

    trial_active = False
    days_left = None
    if doctor.plan_type.value == "trial" and doctor.trial_ends_at:
        delta = (doctor.trial_ends_at.date() - today).days
        trial_active = delta >= 0
        days_left = max(delta, 0)

    # ── Onboarding welcome — show for the first 2 days after registration ──
    # created_at is stored in UTC; compare with utcnow to keep it consistent.
    from datetime import datetime as _dtw, timedelta as _tdw
    show_welcome = bool(
        doctor.created_at and (_dtw.utcnow() - doctor.created_at) < _tdw(days=2)
    )

    # Time-aware greeting — use datetime.now() not date.today() (date has no hour)
    hour = datetime.now().hour
    if hour < 12:
        greeting = "Good Morning"
    elif hour < 17:
        greeting = "Good Afternoon"
    else:
        greeting = "Good Evening"

    # Clinic ownership + primary clinic for display
    # is_clinic_owner is True ONLY if the doctor owns a real Clinic plan (plan_type='clinic'),
    # not just the auto-created trial clinic every solo doctor gets.
    from database.models import ClinicDoctor, Clinic as ClinicModel
    own_membership = (
        db.query(ClinicDoctor)
        .join(ClinicModel, ClinicModel.id == ClinicDoctor.clinic_id)
        .filter(
            ClinicDoctor.doctor_id == doctor.id,
            ClinicDoctor.role == "owner",
            ClinicModel.plan_type == "clinic",
        )
        .first()
    )
    is_clinic_owner = own_membership is not None

    primary_clinic = None
    if own_membership:
        primary_clinic = db.query(ClinicModel).filter(ClinicModel.id == own_membership.clinic_id).first()
    else:
        assoc = db.query(ClinicDoctor).filter(
            ClinicDoctor.doctor_id == doctor.id,
            ClinicDoctor.role == "associate",
            ClinicDoctor.is_active == True,
        ).first()
        if assoc:
            primary_clinic = db.query(ClinicModel).filter(ClinicModel.id == assoc.clinic_id).first()

    # ── Mini income dashboard data ─────────────────────────────────── #
    from datetime import datetime as _dt
    _today_start = _dt.combine(today, _dt.min.time())
    _today_end   = _dt.combine(today, _dt.max.time())

    today_income_row = (
        db.query(func.sum(Bill.total))
        .filter(
            Bill.doctor_id    == doctor.id,
            Bill.paid_at      >= _today_start,
            Bill.paid_at      <= _today_end,
            Bill.payment_mode != PaymentMode.free,
        )
        .scalar()
    )
    today_income = float(today_income_row or 0)

    # Last transaction — TODAY only
    last_bill = (
        db.query(Bill)
        .filter(
            Bill.doctor_id == doctor.id,
            Bill.paid_at   >= _today_start,
            Bill.paid_at   <= _today_end,
        )
        .order_by(Bill.paid_at.desc())
        .first()
    )

    # Recent 5 bills — TODAY only
    recent_bills_dash = (
        db.query(Bill)
        .filter(
            Bill.doctor_id == doctor.id,
            Bill.paid_at   >= _today_start,
            Bill.paid_at   <= _today_end,
        )
        .options(joinedload(Bill.visit).joinedload(Visit.patient))  # eager-load
        .order_by(Bill.paid_at.desc())
        .limit(5)
        .all()
    )

    # ── Pending dues ───────────────────────────────────────────────── #
    # 1) Visits that are BILLING_PENDING — seen but no bill collected yet
    _pending_visits = (
        db.query(Visit)
        .options(joinedload(Visit.patient))   # eager-load — avoids N+1
        .filter(
            Visit.doctor_id == doctor.id,
            Visit.visit_date == today,
            Visit.status     == VisitStatus.billing_pending,
        )
        .all()
    )

    # 2) Bills saved but payment not yet recorded (paid_at is None)
    _unpaid_bills = (
        db.query(Bill)
        .filter(
            Bill.doctor_id    == doctor.id,
            Bill.paid_at      == None,
            Bill.total        >  0,
            Bill.payment_mode != PaymentMode.free,
        )
        .all()
    )

    pending_visits_count  = len(_pending_visits)
    pending_dues_amount   = float(sum(b.total or 0 for b in _unpaid_bills))
    pending_dues_count    = len(_unpaid_bills)

    return templates.TemplateResponse(request, "dashboard.html", {
        "doctor": doctor,
        "today": today,
        "greeting": greeting,
        "todays_appointments": todays_appointments,
        "total_patients": total_patients,
        "total_today": total_today,
        "completed_today":  completed_today,
        "pending_today":    pending_today,
        "walkin_today":     walkin_today,
        "emergency_today":  emergency_today,
        "next_appointment":   next_appointment,
        "past_appointments":  past_appointments,
        "trial_active": trial_active,
        "days_left": days_left,
        "show_welcome": show_welcome,
        "active": "dashboard",
        "is_clinic_owner": is_clinic_owner,
        "primary_clinic": primary_clinic,
        # mini income
        "today_income":        today_income,
        "last_bill":           last_bill,
        "recent_bills_dash":   recent_bills_dash,
        "ExpenseCategory":     ExpenseCategory,
        # pending dues
        "pending_visits_count":  pending_visits_count,
        "pending_visits":        _pending_visits,
        "pending_dues_amount":   pending_dues_amount,
        "pending_dues_count":    pending_dues_count,
        "upgraded":              upgraded == "1",
    })


# ------------------------------------------------------------------ #
#  Settings — GET                                                      #
# ------------------------------------------------------------------ #

@router.get("/doctors/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    doctor: Doctor = Depends(require_pin),
    db: Session = Depends(get_db),
    saved: str = "",
    pin_error: str = "",
    account_error: str = "",
):
    # Build a list of 7 day dicts — each day has a primary shift + optional extra shifts
    days_data = []
    for i, name in enumerate(DAYS):
        rows = (
            db.query(DoctorSchedule)
            .filter(DoctorSchedule.doctor_id == doctor.id, DoctorSchedule.day_of_week == i)
            .order_by(DoctorSchedule.start_time)
            .all()
        )
        s1 = rows[0] if rows else None
        days_data.append({
            "index":        i,
            "name":         name,
            "is_active":    s1.is_active    if s1 else False,
            "start_time":   s1.start_time.strftime("%H:%M") if s1 else "09:00",
            "end_time":     s1.end_time.strftime("%H:%M")   if s1 else "13:00",
            "slot_duration":s1.slot_duration if s1 else 15,
            "max_patients":   s1.max_patients   if s1 else 30,
            "walk_in_buffer": s1.walk_in_buffer if s1 else 0,
            "extra_shifts": [
                {"start": s.start_time.strftime("%H:%M"), "end": s.end_time.strftime("%H:%M")}
                for s in rows[1:]
            ],
        })

    blocked = (
        db.query(BlockedDate)
        .filter(BlockedDate.doctor_id == doctor.id)
        .order_by(BlockedDate.blocked_date)
        .all()
    )
    blocked_times_list = (
        db.query(BlockedTime)
        .filter(BlockedTime.doctor_id == doctor.id)
        .order_by(BlockedTime.blocked_date, BlockedTime.start_time)
        .all()
    )

    from datetime import datetime as dt
    from config import settings as cfg
    now       = dt.utcnow()
    trial_ok  = doctor.trial_ends_at  and doctor.trial_ends_at  > now
    plan_ok   = doctor.plan_expires_at and doctor.plan_expires_at > now

    def days_left(d):
        if not d: return 0
        return max(0, (d - now).days)

    if plan_ok:
        plan_status = doctor.plan_type.value   # "solo", "basic", or "pro"
        plan_days   = days_left(doctor.plan_expires_at)
    elif trial_ok:
        plan_status = "trial"
        plan_days   = days_left(doctor.trial_ends_at)
    else:
        plan_status = "expired"
        plan_days   = 0

    pin_error_msg = {
        "wrong":    "Incorrect current PIN.",
        "mismatch": "PINs do not match.",
        "invalid":  "PIN must be exactly 6 digits.",
    }.get(pin_error, "")

    from database.models import ClinicDoctor, Clinic as ClinicModel2
    _clinic_membership = (
        db.query(ClinicDoctor)
        .join(ClinicModel2, ClinicModel2.id == ClinicDoctor.clinic_id)
        .filter(
            ClinicDoctor.doctor_id == doctor.id,
            ClinicDoctor.role == "owner",
            ClinicDoctor.is_active == True,
            ClinicModel2.plan_type == "clinic",
        )
        .first()
    )
    is_clinic_owner   = _clinic_membership is not None
    is_clinic_account = is_clinic_owner   # True only on real clinic plan

    # Associate doctor — member of a real clinic, access covered by owner
    _assoc_membership = (
        db.query(ClinicDoctor)
        .join(ClinicModel2, ClinicModel2.id == ClinicDoctor.clinic_id)
        .filter(
            ClinicDoctor.doctor_id == doctor.id,
            ClinicDoctor.role == "associate",
            ClinicDoctor.is_active == True,
            ClinicModel2.plan_type == "clinic",
        )
        .first()
    )
    is_clinic_associate = _assoc_membership is not None
    assoc_clinic_name   = None
    if is_clinic_associate and _assoc_membership:
        _ac = db.query(ClinicModel2).filter(ClinicModel2.id == _assoc_membership.clinic_id).first()
        assoc_clinic_name = _ac.name if _ac else None

    price_catalog = (
        db.query(PriceCatalog)
        .filter(PriceCatalog.doctor_id == doctor.id, PriceCatalog.is_active == True)
        .order_by(PriceCatalog.sort_order, PriceCatalog.name)
        .all()
    )

    from database.models import Subscription
    # Latest active clinic subscription (for the active clinic plan card)
    latest_clinic_sub = None
    if is_clinic_account and _clinic_membership:
        latest_clinic_sub = (
            db.query(Subscription)
            .filter(
                Subscription.clinic_id == _clinic_membership.clinic_id,
                Subscription.status == "active",
            )
            .order_by(Subscription.start_date.desc())
            .first()
        )

    account_error_msg = {
        "email_taken": "That email is already in use by another account.",
    }.get(account_error, "")

    return templates.TemplateResponse(request, "settings.html", {
        "doctor":               doctor,
        "days_data":            days_data,
        "blocked_dates":        blocked,
        "blocked_times":        blocked_times_list,
        "saved":                saved == "1",
        "active":               "settings",
        "plan_status":          plan_status,
        "plan_days":            plan_days,
        "razorpay_configured":  bool(cfg.RAZORPAY_KEY_ID),
        "pin_error":            pin_error_msg,
        "account_error":        account_error_msg,
        "pin_required":         getattr(request.state, "pin_required", False),
        "is_clinic_owner":      is_clinic_owner,
        "is_clinic_account":    is_clinic_account,
        "is_clinic_associate":  is_clinic_associate,
        "assoc_clinic_name":    assoc_clinic_name,
        "price_catalog":        price_catalog,
        "latest_clinic_sub":    latest_clinic_sub,
    })


# ------------------------------------------------------------------ #
#  Settings — Save Schedule                                            #
# ------------------------------------------------------------------ #

@router.post("/doctors/settings/schedule", response_class=HTMLResponse)
async def save_schedule(
    request: Request,
    avg_consult_mins: int = Form(10),
    doctor: Doctor = Depends(require_pin),
    db: Session = Depends(get_db),
):
    form = await request.form()

    for i in range(7):
        # Delete all existing schedules for this day (clean slate)
        db.query(DoctorSchedule).filter(
            DoctorSchedule.doctor_id == doctor.id,
            DoctorSchedule.day_of_week == i,
        ).delete(synchronize_session=False)

        if form.get(f"active_{i}") != "on":
            continue   # day is off — leave deleted

        slot_dur    = int(form.get(f"slot_{i}",   15))
        max_pat     = int(form.get(f"max_{i}",    30))
        walk_buf    = max(0, int(form.get(f"walkin_buf_{i}", 0)))

        # Read shifts in order: shift_start_{day}_{k} / shift_end_{day}_{k}
        prev_end = None
        for k in range(20):    # hard cap: 20 shifts per day
            s = (form.get(f"shift_start_{i}_{k}") or "").strip()
            e = (form.get(f"shift_end_{i}_{k}")   or "").strip()
            if not s or not e:
                break          # no more shifts submitted
            try:
                st = dtime.fromisoformat(s)
                et = dtime.fromisoformat(e)
            except ValueError:
                continue
            if et <= st:
                continue       # invalid range
            if prev_end and st < prev_end:
                continue       # overlaps previous shift — skip
            db.add(DoctorSchedule(
                doctor_id=doctor.id, day_of_week=i,
                start_time=st, end_time=et,
                slot_duration=slot_dur, max_patients=max_pat,
                walk_in_buffer=walk_buf,
                is_active=True,
            ))
            prev_end = et

    doctor.avg_consult_mins = max(1, min(120, avg_consult_mins))
    db.commit()
    return RedirectResponse(url="/doctors/settings?saved=1", status_code=303)


# ------------------------------------------------------------------ #
#  Settings — Save Account (name / email / phone)                      #
# ------------------------------------------------------------------ #

@router.post("/doctors/settings/account", response_class=HTMLResponse)
def save_account(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    specialization: str = Form(""),
    medical_reg_number: str = Form(""),
    doctor: Doctor = Depends(require_pin),
    db: Session = Depends(get_db),
):
    name               = name.strip()
    email              = email.strip().lower()
    phone              = phone.strip()
    specialization     = specialization.strip()
    medical_reg_number = medical_reg_number.strip()

    if not name or not email:
        return RedirectResponse(url="/doctors/settings?saved=0", status_code=303)

    # Check email uniqueness (only if changed)
    if email != doctor.email:
        existing = db.query(Doctor).filter(Doctor.email == email, Doctor.id != doctor.id).first()
        if existing:
            return RedirectResponse(url="/doctors/settings?account_error=email_taken", status_code=303)

    doctor.name               = name
    doctor.email              = email
    doctor.phone              = phone or None
    doctor.specialization     = specialization or None
    doctor.medical_reg_number = medical_reg_number or None
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
    clinic_address: str = Form(""),
    languages: str = Form(""),
    doctor: Doctor = Depends(require_pin),
    db: Session = Depends(get_db),
):
    doctor.clinic_name = clinic_name.strip() or None
    doctor.city = city.strip() or None
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
    doctor: Doctor = Depends(require_pin),
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
    doctor: Doctor = Depends(require_pin),
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
#  Settings — Add Blocked Time Range                                   #
# ------------------------------------------------------------------ #

@router.post("/doctors/settings/blocktime", response_class=HTMLResponse)
def add_blocked_time(
    request: Request,
    blocked_date: str  = Form(...),
    start_time:   str  = Form(...),
    end_time:     str  = Form(...),
    reason:       str  = Form(""),
    doctor: Doctor     = Depends(require_pin),
    db: Session        = Depends(get_db),
):
    from datetime import date as date_cls, time as time_cls
    try:
        d  = date_cls.fromisoformat(blocked_date)
        st = time_cls.fromisoformat(start_time)
        et = time_cls.fromisoformat(end_time)
    except ValueError:
        return RedirectResponse(url="/doctors/settings?error=invalid_time", status_code=303)

    if st >= et:
        return RedirectResponse(url="/doctors/settings?error=time_order", status_code=303)

    db.add(BlockedTime(
        doctor_id    = doctor.id,
        blocked_date = d,
        start_time   = st,
        end_time     = et,
        reason       = reason.strip() or None,
    ))
    db.commit()
    return RedirectResponse(url="/doctors/settings?saved=1", status_code=303)


# ------------------------------------------------------------------ #
#  Settings — Remove Blocked Time Range                                #
# ------------------------------------------------------------------ #

@router.post("/doctors/settings/unblocktime/{bt_id}", response_class=HTMLResponse)
def remove_blocked_time(
    bt_id:  int,
    doctor: Doctor  = Depends(require_pin),
    db: Session     = Depends(get_db),
):
    record = db.query(BlockedTime).filter(
        BlockedTime.id        == bt_id,
        BlockedTime.doctor_id == doctor.id,
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
    doctor: Doctor = Depends(get_paying_doctor),
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


# ------------------------------------------------------------------ #
#  Reports                                                             #
# ------------------------------------------------------------------ #

@router.get("/reports", response_class=HTMLResponse)
def reports_page(
    request: Request,
    doctor: Doctor = Depends(require_pin),
    db: Session = Depends(get_db),
):
    import json
    from datetime import timedelta
    today = date.today()

    # ---- This week vs last week ----
    start_of_week      = today - timedelta(days=today.weekday())
    start_of_last_week = start_of_week - timedelta(days=7)
    end_of_last_week   = start_of_week - timedelta(days=1)

    this_week = db.query(func.count(Appointment.id)).filter(
        Appointment.doctor_id == doctor.id,
        Appointment.appointment_date >= start_of_week,
        Appointment.appointment_date <= today,
        Appointment.status != AppointmentStatus.cancelled,
    ).scalar() or 0

    last_week = db.query(func.count(Appointment.id)).filter(
        Appointment.doctor_id == doctor.id,
        Appointment.appointment_date >= start_of_last_week,
        Appointment.appointment_date <= end_of_last_week,
        Appointment.status != AppointmentStatus.cancelled,
    ).scalar() or 0

    # ---- Completion & no-show rates (single query, conditional aggregation) ----
    _rate_counts = db.query(
        func.count(case((Appointment.status == AppointmentStatus.completed, 1))).label("completed"),
        func.count(case((Appointment.status == AppointmentStatus.no_show,   1))).label("no_show"),
    ).filter(
        Appointment.doctor_id == doctor.id,
        Appointment.status.in_([
            AppointmentStatus.completed,
            AppointmentStatus.no_show,
        ]),
    ).one()

    completed_count = _rate_counts.completed or 0
    no_show_count   = _rate_counts.no_show   or 0
    past_total      = completed_count + no_show_count

    completion_rate = round(completed_count / past_total * 100) if past_total else 0
    no_show_rate    = round(no_show_count   / past_total * 100) if past_total else 0

    # ---- Average wait time (check_in → call) for this week vs last week ----
    # The most operationally important UX metric for any clinic.
    def _avg_wait_mins(visit_date_from: date, visit_date_to: date) -> int | None:
        rows = (
            db.query(Visit.check_in_time, Visit.call_time)
            .filter(
                Visit.doctor_id == doctor.id,
                Visit.visit_date >= visit_date_from,
                Visit.visit_date <  visit_date_to,
                Visit.check_in_time.isnot(None),
                Visit.call_time.isnot(None),
            )
            .all()
        )
        if not rows:
            return None
        total_secs = sum(
            (call - chk).total_seconds()
            for chk, call in rows
            if call and chk and call > chk
        )
        # Filter out visits where call == check_in (instant calls) or negative.
        valid = [(chk, call) for chk, call in rows if call and chk and call > chk]
        if not valid:
            return None
        avg_secs = total_secs / len(valid)
        return max(0, int(round(avg_secs / 60)))

    # Week-aligned: current week starts Mon
    _today_weekday = today.weekday()
    week_start  = today - timedelta(days=_today_weekday)
    week_end    = week_start + timedelta(days=7)
    prev_start  = week_start - timedelta(days=7)
    avg_wait_this_week = _avg_wait_mins(week_start, week_end)
    avg_wait_last_week = _avg_wait_mins(prev_start, week_start)
    # Trend delta — positive = got worse, negative = got faster
    if avg_wait_this_week is not None and avg_wait_last_week:
        wait_delta_mins = avg_wait_this_week - avg_wait_last_week
        wait_delta_pct  = round((wait_delta_mins / avg_wait_last_week) * 100)
    else:
        wait_delta_mins = None
        wait_delta_pct  = None

    # ---- Peak-hours heatmap (last 30 days, Mon-Sun × hour-of-day) ----
    # Counts visits by weekday + hour of check_in_time so doctors can see
    # when they're busiest and plan staffing / blocked time accordingly.
    heatmap_from = today - timedelta(days=30)
    visit_rows = (
        db.query(Visit.check_in_time)
        .filter(
            Visit.doctor_id == doctor.id,
            Visit.check_in_time.isnot(None),
            Visit.visit_date >= heatmap_from,
        )
        .all()
    )
    # Fixed 8am – 10pm band (14 columns) — keeps the grid visually consistent
    # regardless of how sparse the data is. Out-of-band visits get bucketed
    # into the nearest edge column so they aren't silently dropped.
    # IST clinics often run evening hours up to 9–10 PM; 8 PM cutoff was
    # clamping late-evening visits into the 7 PM bar.
    peak_hour_start = 8
    peak_hour_end   = 22

    hour_labels = list(range(peak_hour_start, peak_hour_end))
    # Total visits per hour across the whole 30-day window. One bar per hour.
    hour_counts = [0] * len(hour_labels)
    for v in visit_rows:
        if not v.check_in_time:
            continue
        h = v.check_in_time.hour
        # Clamp out-of-band hours to the nearest edge so off-hours visits show
        if h < peak_hour_start: h = peak_hour_start
        if h >= peak_hour_end:  h = peak_hour_end - 1
        hour_counts[h - peak_hour_start] += 1
    peak_max = max(hour_counts) if hour_counts else 0

    # ---- Top 5 patients ----
    top_patients = (
        db.query(Patient, func.count(Appointment.id).label("cnt"))
        .join(Appointment, Patient.id == Appointment.patient_id)
        .filter(
            Appointment.doctor_id == doctor.id,
            Appointment.status != AppointmentStatus.cancelled,
        )
        .group_by(Patient.id)
        .order_by(func.count(Appointment.id).desc())
        .limit(5)
        .all()
    )

    # ---- Visit type breakdown ----
    type_rows = (
        db.query(Appointment.appointment_type, func.count(Appointment.id).label("cnt"))
        .filter(
            Appointment.doctor_id == doctor.id,
            Appointment.status != AppointmentStatus.cancelled,
        )
        .group_by(Appointment.appointment_type)
        .all()
    )
    type_total = sum(r.cnt for r in type_rows) or 1
    type_breakdown = [
        {
            "label": r.appointment_type.value.replace("_", " ").title(),
            "count": r.cnt,
            "pct":   round(r.cnt / type_total * 100),
        }
        for r in type_rows
    ]

    # ---- Source breakdown (marketing attribution per patient) ----
    # Aggregate distinct patients per first-touch source. Patients with no
    # source set are excluded from the pct denominator and shown separately
    # in the card subtitle so the bars are meaningful.
    _src_labels = {
        ReferralSource.instagram:       "Instagram",
        ReferralSource.facebook:        "Facebook",
        ReferralSource.youtube:         "YouTube",
        ReferralSource.google:          "Google",
        ReferralSource.pamphlet:        "Pamphlet",
        ReferralSource.hoarding:        "Hoarding",
        ReferralSource.referral_friend: "Referral",
        ReferralSource.walk_by:         "Walked by",
        ReferralSource.other:           "Other",
    }
    src_rows = (
        db.query(Patient.referral_source, func.count(Patient.id).label("cnt"))
        .filter(
            Patient.doctor_id == doctor.id,
            Patient.referral_source.isnot(None),
        )
        .group_by(Patient.referral_source)
        .all()
    )
    src_total = sum(r.cnt for r in src_rows) or 1
    source_breakdown = sorted(
        [
            {
                "label": _src_labels.get(r.referral_source, r.referral_source.value.title()),
                "count": r.cnt,
                "pct":   round(r.cnt / src_total * 100),
            }
            for r in src_rows
        ],
        key=lambda x: x["count"], reverse=True,
    )
    source_known_count = sum(r.cnt for r in src_rows)
    source_unknown_count = (
        db.query(func.count(Patient.id))
        .filter(
            Patient.doctor_id == doctor.id,
            Patient.referral_source.is_(None),
        )
        .scalar() or 0
    )

    # ---- New patients this month vs last ----
    start_this_month = today.replace(day=1)
    start_last_month = (start_this_month - timedelta(days=1)).replace(day=1)

    patients_this_month = db.query(func.count(Patient.id)).filter(
        Patient.doctor_id == doctor.id,
        Patient.created_at >= start_this_month,
    ).scalar() or 0

    patients_last_month = db.query(func.count(Patient.id)).filter(
        Patient.doctor_id == doctor.id,
        Patient.created_at >= start_last_month,
        Patient.created_at < start_this_month,
    ).scalar() or 0

    return templates.TemplateResponse(request, "reports.html", {
        "doctor":              doctor,
        "today":               today,
        "this_week":           this_week,
        "last_week":           last_week,
        "completion_rate":     completion_rate,
        "no_show_rate":        no_show_rate,
        # Wait-time stat
        "avg_wait_this_week":  avg_wait_this_week,
        "avg_wait_last_week":  avg_wait_last_week,
        "wait_delta_mins":     wait_delta_mins,
        "wait_delta_pct":      wait_delta_pct,
        # Peak-hours bar chart
        "peak_hour_labels":    hour_labels,
        "peak_hour_counts":    hour_counts,
        "peak_max":            peak_max,
        "top_patients":        top_patients,
        "type_breakdown":      type_breakdown,
        "source_breakdown":    source_breakdown,
        "source_known_count":  source_known_count,
        "source_unknown_count": source_unknown_count,
        "patients_this_month": patients_this_month,
        "patients_last_month": patients_last_month,
        "active":              "reports",
        "pin_required":        getattr(request.state, "pin_required", False),
    })


# ------------------------------------------------------------------ #
#  Billing                                                             #
# ------------------------------------------------------------------ #

# ── Public pricing page (no auth required) ────────────────────────────────

@router.get("/pricing", response_class=HTMLResponse)
def pricing_page(
    request: Request,
    db: Session = Depends(get_db),
):
    from services.payment_service import PLAN_CONFIG
    from config import settings as cfg
    from database.models import ClinicDoctor
    doctor = None
    try:
        from services.auth_service import get_current_doctor
        doctor = get_current_doctor(request, db)
    except Exception:
        pass

    # Show Enterprise card only when the doctor's clinic has > 6 members
    show_enterprise = False
    if doctor:
        membership = (
            db.query(ClinicDoctor)
            .filter(ClinicDoctor.doctor_id == doctor.id, ClinicDoctor.is_active == True)
            .first()
        )
        if membership:
            doctor_count = (
                db.query(ClinicDoctor)
                .filter(ClinicDoctor.clinic_id == membership.clinic_id, ClinicDoctor.is_active == True)
                .count()
            )
            show_enterprise = doctor_count > 6

    plan_status = None
    if doctor:
        now = datetime.utcnow()
        if doctor.trial_ends_at and doctor.trial_ends_at > now:
            plan_status = "trial"
        elif doctor.plan_expires_at and doctor.plan_expires_at > now:
            plan_status = "active"
        else:
            plan_status = "expired"

    current_plan = doctor.plan_type.value if doctor and doctor.plan_type else "trial"

    # Minimum seats the doctor actually needs right now (based on clinic headcount).
    # Solo doctors → 1.  Clinic owners → number of active members in their clinic.
    # This prevents buying a plan with fewer seats than doctors already in the clinic.
    min_seats_needed = 1
    if doctor:
        owned = (
            db.query(ClinicDoctor)
            .filter(ClinicDoctor.doctor_id == doctor.id, ClinicDoctor.role == "owner")
            .first()
        )
        if owned:
            member_count = (
                db.query(ClinicDoctor)
                .filter(ClinicDoctor.clinic_id == owned.clinic_id,
                        ClinicDoctor.is_active == True)
                .count()
            )
            min_seats_needed = max(1, member_count)

    return templates.TemplateResponse(request, "pricing.html", {
        "plans":               PLAN_CONFIG,
        "razorpay_configured": bool(cfg.RAZORPAY_KEY_ID),
        "doctor":              doctor,
        "show_enterprise":     show_enterprise,
        "plan_status":         plan_status,
        "current_plan":        current_plan,
        "min_seats_needed":    min_seats_needed,
        "active":              "",
    })


@router.get("/billing", response_class=HTMLResponse)
def billing_page(
    request: Request,
    success: str = Query(default=""),
    doctor: Doctor = Depends(require_pin_auth),
    db: Session = Depends(get_db),
):
    from datetime import datetime as dt
    from config import settings as cfg
    from services.payment_service import PLAN_CONFIG

    now = dt.utcnow()
    trial_ok   = doctor.trial_ends_at  and doctor.trial_ends_at  > now
    plan_ok    = doctor.plan_expires_at and doctor.plan_expires_at > now
    is_expired = not trial_ok and not plan_ok

    def days_left(dt_obj):
        if not dt_obj:
            return 0
        return max(0, (dt_obj - now).days)

    # Active plan label + seat info
    current_plan_key  = doctor.plan_type.value if doctor.plan_type else "trial"
    current_plan_cfg  = PLAN_CONFIG.get(current_plan_key)
    current_plan_label = (
        current_plan_cfg["label"] if current_plan_cfg
        else current_plan_key.title()
    )

    # Latest subscription row for receipt details
    from database.models import Subscription, ClinicDoctor
    latest_sub = (
        db.query(Subscription)
        .filter(Subscription.doctor_id == doctor.id, Subscription.status == "active")
        .order_by(Subscription.id.desc())
        .first()
    )

    # Show Enterprise card only when the doctor's clinic has > 6 members
    show_enterprise = False
    min_seats_needed = 1
    membership = (
        db.query(ClinicDoctor)
        .filter(ClinicDoctor.doctor_id == doctor.id, ClinicDoctor.is_active == True)
        .first()
    )
    if membership:
        doctor_count = (
            db.query(ClinicDoctor)
            .filter(ClinicDoctor.clinic_id == membership.clinic_id, ClinicDoctor.is_active == True)
            .count()
        )
        show_enterprise = doctor_count > 6

    # Seat-based gating — clinic owners can't accidentally buy Solo
    owned = (
        db.query(ClinicDoctor)
        .filter(ClinicDoctor.doctor_id == doctor.id, ClinicDoctor.role == "owner")
        .first()
    )
    if owned:
        member_count = (
            db.query(ClinicDoctor)
            .filter(ClinicDoctor.clinic_id == owned.clinic_id, ClinicDoctor.is_active == True)
            .count()
        )
        min_seats_needed = max(1, member_count)

    PLAN_RANK = {"trial": 0, "solo": 1, "duo": 2, "clinic": 3, "hospital": 4, "enterprise": 5}
    current_plan_rank = PLAN_RANK.get(current_plan_key, 0)

    return templates.TemplateResponse(request, "billing.html", {
        "doctor":              doctor,
        "trial_ok":            trial_ok,
        "plan_ok":             plan_ok,
        "is_expired":          is_expired,
        "trial_days_left":     days_left(doctor.trial_ends_at),
        "plan_days_left":      days_left(doctor.plan_expires_at),
        "razorpay_configured": bool(cfg.RAZORPAY_KEY_ID),
        "success":             success,
        "active":              "billing",
        "pin_required":        getattr(request.state, "pin_required", False),
        "current_plan_key":    current_plan_key,
        "current_plan_label":  current_plan_label,
        "current_plan_cfg":    current_plan_cfg,
        "current_plan_rank":   current_plan_rank,
        "latest_sub":          latest_sub,
        "plan_config":         PLAN_CONFIG,
        "show_enterprise":     show_enterprise,
        "min_seats_needed":    min_seats_needed,
    })


@router.post("/billing/create-order")
def billing_create_order(
    plan: str = Query(...),
    doctor: Doctor = Depends(get_current_doctor),
):
    from fastapi.responses import JSONResponse
    from services.payment_service import create_order
    result = create_order(plan)
    return JSONResponse(result)


@router.post("/billing/verify", response_class=HTMLResponse)
def billing_verify(
    razorpay_payment_id: str = Form(...),
    razorpay_order_id:   str = Form(...),
    razorpay_signature:  str = Form(...),
    plan:                str = Form(...),
    doctor: Doctor = Depends(get_current_doctor),
    db: Session    = Depends(get_db),
):
    from datetime import datetime as dt, timedelta
    from services.payment_service import verify_signature, PLAN_AMOUNTS, PLAN_CONFIG
    from database.models import Subscription, PlanType

    if not verify_signature(razorpay_payment_id, razorpay_order_id, razorpay_signature):
        return RedirectResponse(url="/billing?success=fail", status_code=303)

    now      = dt.utcnow()
    end_date = now + timedelta(days=30)

    cfg = PLAN_CONFIG.get(plan, {})
    seats = cfg.get("seats")  # None = unlimited

    # Map plan string → PlanType enum (fall back to solo for unknown/legacy)
    plan_type_map = {
        "solo":       PlanType.solo,
        "duo":        PlanType.duo,
        "clinic":     PlanType.clinic,
        "hospital":   PlanType.hospital,
        "enterprise": PlanType.enterprise,
        "basic":      PlanType.basic,
        "pro":        PlanType.pro,
    }

    sub = Subscription(
        doctor_id  = doctor.id,
        plan_name  = plan,
        amount     = PLAN_AMOUNTS.get(plan, 0),
        payment_id = razorpay_payment_id,
        start_date = now.date(),
        end_date   = end_date.date(),
        status     = "active",
    )
    db.add(sub)

    doctor.plan_expires_at = end_date
    doctor.plan_type       = plan_type_map.get(plan, PlanType.solo)
    doctor.plan_seats      = seats  # None = unlimited

    # ── Sync the owned Clinic record ──────────────────────────────────
    # is_clinic_owner checks filter on Clinic.plan_type == "clinic", so
    # we must update the clinic row whenever a multi-doctor plan is purchased.
    from database.models import ClinicDoctor as _ClinicDoctor, Clinic as _ClinicModel
    multi_doctor_plans = {"duo", "clinic", "hospital", "enterprise"}
    _owned = (
        db.query(_ClinicDoctor)
        .filter(
            _ClinicDoctor.doctor_id == doctor.id,
            _ClinicDoctor.role == "owner",
        )
        .first()
    )
    if _owned:
        _clinic = db.query(_ClinicModel).filter(_ClinicModel.id == _owned.clinic_id).first()
        if _clinic:
            if plan in multi_doctor_plans:
                _clinic.plan_type      = "clinic"
                _clinic.plan_expires_at = end_date
            else:
                # Downgraded / solo plan — strip clinic-tier features
                _clinic.plan_type      = "trial"
                _clinic.plan_expires_at = None

    db.commit()
    return RedirectResponse(url="/dashboard?upgraded=1", status_code=303)


# ------------------------------------------------------------------ #
#  PIN Prompt — GET (show entry form)                                  #
# ------------------------------------------------------------------ #

@router.get("/pin-prompt", response_class=HTMLResponse)
def pin_prompt_page(
    next: str = Query(default="/dashboard"),
):
    # Overlay is now inline on each protected page.
    # This route just redirects to the destination (which will show the overlay).
    return RedirectResponse(url=next, status_code=303)


# ------------------------------------------------------------------ #
#  PIN Prompt — POST (verify and set cookie)                           #
# ------------------------------------------------------------------ #

@router.post("/pin-prompt", response_class=HTMLResponse)
async def verify_pin_post(
    request: Request,
    # default="" not Form(...): an empty PIN field is dropped entirely by
    # Starlette's form parser (blank values aren't kept), so a required
    # Form(...) sees it as *missing* and raises a raw 422 instead of just
    # failing the PIN check like every other wrong guess does.
    pin: str = Form(default=""),
    next: str = Form(default="/dashboard"),
    doctor: Doctor = Depends(get_current_doctor),
):
    if not doctor.pin_hash:
        return RedirectResponse(url=next, status_code=303)

    if not verify_password(pin.strip(), doctor.pin_hash):
        from urllib.parse import quote
        # Redirect back to the same page — the overlay will show with error
        sep = "&" if "?" in next else "?"
        return RedirectResponse(url=f"{next}{sep}pin_error=1", status_code=303)

    resp = RedirectResponse(url=next, status_code=303)
    token = create_pin_token(doctor.id)
    resp.set_cookie("pin_session", token, httponly=True, secure=settings.ENVIRONMENT.lower() == "production", samesite="lax", max_age=1800)
    return resp


# ------------------------------------------------------------------ #
#  Settings — PIN Setup / Change / Remove                              #
# ------------------------------------------------------------------ #

@router.post("/doctors/settings/pin", response_class=HTMLResponse)
async def update_pin(
    request: Request,
    current_pin: str = Form(""),
    new_pin: str = Form(""),
    confirm_pin: str = Form(""),
    action: str = Form("set"),
    doctor: Doctor = Depends(get_paying_doctor),   # not require_pin — PIN setup is the entry point
    db: Session = Depends(get_db),
):
    if action == "remove":
        if not doctor.pin_hash:
            return RedirectResponse("/doctors/settings?saved=1", 303)
        if not verify_password(current_pin.strip(), doctor.pin_hash):
            return RedirectResponse("/doctors/settings?pin_error=wrong", 303)
        doctor.pin_hash = None
        db.commit()
        resp = RedirectResponse("/doctors/settings?saved=1", 303)
        resp.delete_cookie("pin_session")
        return resp

    # Validate new PIN
    pin = new_pin.strip()
    confirm = confirm_pin.strip()
    if not pin.isdigit() or len(pin) != 6:
        return RedirectResponse("/doctors/settings?pin_error=invalid", 303)
    if pin != confirm:
        return RedirectResponse("/doctors/settings?pin_error=mismatch", 303)
    if doctor.pin_hash and not verify_password(current_pin.strip(), doctor.pin_hash):
        return RedirectResponse("/doctors/settings?pin_error=wrong", 303)

    doctor.pin_hash = hash_password(pin)
    db.commit()

    # Issue pin_session so the doctor stays verified after setting PIN
    resp = RedirectResponse("/doctors/settings?saved=1", 303)
    token = create_pin_token(doctor.id)
    resp.set_cookie("pin_session", token, httponly=True, secure=settings.ENVIRONMENT.lower() == "production", samesite="lax", max_age=1800)
    return resp
