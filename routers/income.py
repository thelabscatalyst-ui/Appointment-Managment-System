"""
routers/income.py — Income Dashboard + Expense Tracker

Routes:
  GET  /income                         KPI strip, 30-day chart, breakdowns, pending
  GET  /expenses                       Expense list + add form + recurring rules
  POST /expenses                       Add a one-off expense
  POST /expenses/{id}/delete           Delete expense
  POST /expenses/recurring             Add recurring rule
  POST /expenses/recurring/{id}/toggle Toggle active/inactive
  POST /expenses/recurring/{id}/delete Delete recurring rule
"""

from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from database.connection import get_db
from database.models import (
    Appointment,
    Bill,
    Doctor,
    Expense,
    ExpenseCategory,
    Patient,
    PaymentMode,
    RecurringExpense,
)
from services.auth_service import get_paying_doctor, require_pin

router    = APIRouter(tags=["income"])
templates = Jinja2Templates(directory="templates")


# ── small helpers ──────────────────────────────────────────────────────────── #

def _f(val) -> float:
    """Safe float cast from Decimal / None."""
    return float(val or 0)


def _month_range(year: int, month: int):
    _, last = monthrange(year, month)
    return date(year, month, 1), date(year, month, last)


def _dt_start(d: date) -> datetime:
    return datetime.combine(d, datetime.min.time())


def _dt_end(d: date) -> datetime:
    return datetime.combine(d, datetime.max.time())


# ── recurring-expense auto-fire ────────────────────────────────────────────── #

def _fire_due_recurring(doctor_id: int, db: Session) -> None:
    """
    Create Expense rows for every RecurringExpense that is due today or overdue
    and has not yet been created this calendar month.  Called on page load.
    """
    today = date.today()
    rules = (
        db.query(RecurringExpense)
        .filter(
            RecurringExpense.doctor_id == doctor_id,
            RecurringExpense.is_active == True,
        )
        .all()
    )
    dirty = False
    for rule in rules:
        due_day         = min(rule.day_of_month, 28)
        due_this_month  = date(today.year, today.month, due_day)
        if today < due_this_month:
            continue
        already = (
            db.query(Expense)
            .filter(
                Expense.recurring_id == rule.id,
                Expense.expense_date >= date(today.year, today.month, 1),
            )
            .first()
        )
        if not already:
            db.add(Expense(
                doctor_id    = rule.doctor_id,
                clinic_id    = rule.clinic_id,
                category     = rule.category,
                amount       = rule.amount,
                expense_date = due_this_month,
                description  = rule.label + " (auto)",
                recurring_id = rule.id,
            ))
            dirty = True
    if dirty:
        db.commit()


# ── income sum helper ──────────────────────────────────────────────────────── #

def _income_sum(doctor_id: int, start: date, end: date, db: Session,
                clinic_id: int | None = None) -> float:
    """Revenue belongs to the clinic the work happened at, never to the doctor
    personally — an associate's takings at someone else's clinic must not
    appear in their own P&L."""
    q = (
        db.query(func.sum(Bill.total))
        .filter(
            Bill.doctor_id == doctor_id,
            Bill.paid_at   >= _dt_start(start),
            Bill.paid_at   <= _dt_end(end),
            Bill.payment_mode != PaymentMode.free,
        )
    )
    if clinic_id is not None:
        q = q.filter(Bill.clinic_id == clinic_id)
    return _f(q.scalar())


def _expense_sum(doctor_id: int, start: date, end: date, db: Session,
                 clinic_id: int | None = None) -> float:
    q = (
        db.query(func.sum(Expense.amount))
        .filter(
            Expense.doctor_id    == doctor_id,
            Expense.expense_date >= start,
            Expense.expense_date <= end,
        )
    )
    if clinic_id is not None:
        q = q.filter(Expense.clinic_id == clinic_id)
    return _f(q.scalar())


# ═══════════════════════════════════════════════════════════════════════════════
#  GET /income  — dashboard
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/income", response_class=HTMLResponse)
async def income_dashboard(
    request: Request,
    db:      Session = Depends(get_db),
    doctor:  Doctor  = Depends(require_pin),
):
    # Money is attributed to the clinic the work happened at.
    _acid_scope = getattr(request.state, "active_clinic_id", None)
    _fire_due_recurring(doctor.id, db)

    # Money is attributed to the clinic the work happened at.
    _acid = getattr(request.state, "active_clinic_id", None)

    today      = date.today()
    this_year  = today.year
    this_month = today.month

    lm_date   = today.replace(day=1) - timedelta(days=1)
    lm_month  = lm_date.month
    lm_year   = lm_date.year

    m_first,  m_last  = _month_range(this_year,  this_month)
    lm_first, lm_last = _month_range(lm_year, lm_month)

    # ── KPI: income ───────────────────────────────────────────────────── #
    today_income      = _income_sum(doctor.id, today,  today,  db, _acid)
    month_income      = _income_sum(doctor.id, m_first, m_last, db, _acid)
    last_month_income = _income_sum(doctor.id, lm_first, lm_last, db, _acid)
    year_income       = _income_sum(doctor.id, date(this_year,1,1), date(this_year,12,31), db, _acid)

    mom_pct = (
        ((month_income - last_month_income) / last_month_income * 100)
        if last_month_income > 0 else 0.0
    )

    # ── KPI: expenses ─────────────────────────────────────────────────── #
    month_expense      = _expense_sum(doctor.id, m_first, m_last, db, _acid)
    last_month_expense = _expense_sum(doctor.id, lm_first, lm_last, db, _acid)
    year_expense       = _expense_sum(doctor.id, date(this_year,1,1), date(this_year,12,31), db, _acid)

    pnl_month      = month_income      - month_expense
    pnl_last_month = last_month_income - last_month_expense

    # ── Pending collections ───────────────────────────────────────────── #
    pending_bills = (
        db.query(Bill)
        .filter(
            Bill.doctor_id    == doctor.id,
            Bill.clinic_id    == _acid_scope,
            Bill.paid_amount  == 0,
            Bill.total        > 0,
            Bill.payment_mode != PaymentMode.free,
        )
        .order_by(Bill.created_at.desc())
        .all()
    )
    pending_amount = sum(_f(b.total) for b in pending_bills)

    # ── 30-day daily revenue ──────────────────────────────────────────── #
    chart_start = today - timedelta(days=29)
    bills_30d = (
        db.query(Bill)
        .filter(
            Bill.doctor_id == doctor.id,
            Bill.clinic_id == _acid_scope,
            Bill.paid_at   >= _dt_start(chart_start),
            Bill.paid_at   <= _dt_end(today),
            Bill.payment_mode != PaymentMode.free,
        )
        .all()
    )
    daily_map: dict[str, float] = defaultdict(float)
    for b in bills_30d:
        if b.paid_at:
            daily_map[b.paid_at.strftime("%Y-%m-%d")] += _f(b.total)

    chart_days = []
    for i in range(30):
        d = chart_start + timedelta(days=i)
        chart_days.append({
            "date":   d.strftime("%d %b"),
            "key":    d.strftime("%Y-%m-%d"),
            "amount": daily_map.get(d.strftime("%Y-%m-%d"), 0.0),
            "is_today": d == today,
        })

    # ── Payment mode breakdown (this month) ──────────────────────────── #
    mode_rows = (
        db.query(Bill.payment_mode, func.sum(Bill.total).label("t"))
        .filter(
            Bill.doctor_id == doctor.id,
            Bill.clinic_id == _acid_scope,
            Bill.paid_at   >= _dt_start(m_first),
            Bill.paid_at   <= _dt_end(m_last),
        )
        .group_by(Bill.payment_mode)
        .all()
    )
    mode_breakdown = sorted(
        [
            {
                "label":  (m.value.title() if m else "Unknown"),
                "amount": _f(t),
                "pct":    round(_f(t) / month_income * 100) if month_income > 0 else 0,
            }
            for m, t in mode_rows
        ],
        key=lambda x: x["amount"], reverse=True,
    )

    # ── Visit-type breakdown (bill → visit → appointment) this month ─── #
    bills_month = (
        db.query(Bill)
        .filter(
            Bill.doctor_id == doctor.id,
            Bill.clinic_id == _acid_scope,
            Bill.paid_at   >= _dt_start(m_first),
            Bill.paid_at   <= _dt_end(m_last),
            Bill.payment_mode != PaymentMode.free,
        )
        .all()
    )
    type_map: dict[str, float] = defaultdict(float)
    for b in bills_month:
        label = "Walk-in"
        if b.visit and b.visit.appointment_id:
            appt = db.query(Appointment).filter(
                Appointment.id == b.visit.appointment_id
            ).first()
            if appt:
                label = appt.appointment_type.value.replace("_", " ").title()
        type_map[label] += _f(b.total)

    type_total = sum(type_map.values()) or 1
    type_breakdown = sorted(
        [
            {"label": k, "amount": v,
             "pct": round(v / type_total * 100)}
            for k, v in type_map.items()
        ],
        key=lambda x: x["amount"], reverse=True,
    )

    # ── Day-of-week breakdown (last 90 days) ─────────────────────────── #
    bills_90d = (
        db.query(Bill)
        .filter(
            Bill.doctor_id == doctor.id,
            Bill.clinic_id == _acid_scope,
            Bill.paid_at   >= _dt_start(today - timedelta(days=89)),
            Bill.paid_at   <= _dt_end(today),
            Bill.payment_mode != PaymentMode.free,
        )
        .all()
    )
    dow_names   = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    dow_totals  = [0.0] * 7
    for b in bills_90d:
        if b.paid_at:
            dow_totals[b.paid_at.weekday()] += _f(b.total)
    max_dow = max(dow_totals) or 1
    dow_breakdown = [
        {
            "label":  dow_names[i],
            "amount": dow_totals[i],
            "pct":    round(dow_totals[i] / max_dow * 100),
        }
        for i in range(7)
    ]

    # ── Expense breakdown by category (this month) ───────────────────── #
    exp_rows = (
        db.query(Expense.category, func.sum(Expense.amount).label("t"))
        .filter(
            Expense.doctor_id    == doctor.id,
            Expense.clinic_id == _acid_scope,
            Expense.expense_date >= m_first,
            Expense.expense_date <= m_last,
        )
        .group_by(Expense.category)
        .all()
    )
    exp_breakdown = sorted(
        [
            {
                "label":  (c.value.title() if c else "Misc"),
                "amount": _f(t),
                "pct":    round(_f(t) / month_expense * 100) if month_expense > 0 else 0,
            }
            for c, t in exp_rows
        ],
        key=lambda x: x["amount"], reverse=True,
    )

    # ── Top patients this month ───────────────────────────────────────── #
    top_pts = (
        db.query(Patient, func.sum(Bill.total).label("total"))
        .join(Bill, Bill.patient_id == Patient.id)
        .filter(
            Bill.doctor_id == doctor.id,
            Bill.clinic_id == _acid_scope,
            Bill.paid_at   >= _dt_start(m_first),
            Bill.paid_at   <= _dt_end(m_last),
            Bill.payment_mode != PaymentMode.free,
        )
        .group_by(Patient.id)
        .order_by(func.sum(Bill.total).desc())
        .limit(5)
        .all()
    )

    # ── Recent 5 bills for dashboard strip ───────────────────────────── #
    recent_bills = (
        db.query(Bill)
        .filter(Bill.doctor_id == doctor.id, Bill.clinic_id == _acid_scope)
        .order_by(Bill.paid_at.desc().nullslast(), Bill.created_at.desc())
        .limit(5)
        .all()
    )

    return templates.TemplateResponse(request, "income.html", {
        "active":             "income",
        "doctor":             doctor,
        "today":              today,
        "pin_required":       getattr(request.state, "pin_required", False),
        # income KPIs
        "today_income":       today_income,
        "month_income":       month_income,
        "last_month_income":  last_month_income,
        "year_income":        year_income,
        "mom_pct":            mom_pct,
        # expense KPIs
        "month_expense":      month_expense,
        "last_month_expense": last_month_expense,
        "year_expense":       year_expense,
        # P&L
        "pnl_month":          pnl_month,
        "pnl_last_month":     pnl_last_month,
        # pending
        "pending_bills":      pending_bills,
        "pending_amount":     pending_amount,
        # chart
        "chart_days":         chart_days,
        # breakdowns
        "mode_breakdown":     mode_breakdown,
        "type_breakdown":     type_breakdown,
        "dow_breakdown":      dow_breakdown,
        "exp_breakdown":      exp_breakdown,
        # tables
        "top_patients":       top_pts,
        "recent_bills":       recent_bills,
    })


# ═══════════════════════════════════════════════════════════════════════════════
#  GET /income/transactions  — paginated all-payments page
# ═══════════════════════════════════════════════════════════════════════════════

import math as _math
from fastapi import Query as _Q

TXN_PER_PAGE = 10

@router.get("/income/transactions", response_class=HTMLResponse)
async def transactions_page(
    request: Request,
    month:   int = _Q(default=0),
    year:    int = _Q(default=0),
    page:    int = _Q(default=1),
    view:    str = _Q(default="monthly"),   # "monthly" | "yearly"
    db:      Session = Depends(get_db),
    doctor:  Doctor  = Depends(require_pin),
):
    # Money is attributed to the clinic the work happened at.
    _acid_scope = getattr(request.state, "active_clinic_id", None)
    import calendar as _cal
    today = date.today()
    if not year:  year  = today.year
    if not month: month = today.month

    # ── Build year list (all years that have bills + current year) ──────────
    all_bill_dates = (
        db.query(Bill.paid_at, Bill.created_at)
        .filter(Bill.doctor_id == doctor.id, Bill.clinic_id == _acid_scope)
        .all()
    )
    year_set  = {today.year}
    month_set = {(today.year, today.month)}
    for b_paid, b_created in all_bill_dates:
        d = b_paid or b_created
        if d:
            year_set.add(d.year)
            month_set.add((d.year, d.month))
    year_list  = sorted(year_set,  reverse=True)
    # months grouped by year for the monthly selector
    months_by_year: dict[int, list[int]] = defaultdict(list)
    for y, m in sorted(month_set, reverse=True):
        months_by_year[y].append(m)

    # ────────────────────────────────────────────────────────────────────────
    #  YEARLY VIEW
    # ────────────────────────────────────────────────────────────────────────
    if view == "yearly":
        yearly_months = []
        year_grand_total = 0.0
        year_txn_count   = 0

        for mo in range(1, 13):
            mf, ml = _month_range(year, mo)
            mo_bills = (
                db.query(Bill)
                .filter(
                    Bill.doctor_id == doctor.id,
                    Bill.clinic_id == _acid_scope,
                    Bill.paid_at   >= _dt_start(mf),
                    Bill.paid_at   <= _dt_end(ml),
                )
                .all()
            )
            mo_total  = sum(_f(b.total) for b in mo_bills if b.payment_mode and b.payment_mode.value != "free")
            mo_count  = len(mo_bills)

            # mode breakdown for this month
            mt: dict[str, float] = defaultdict(float)
            mc: dict[str, int]   = defaultdict(int)
            for b in mo_bills:
                lbl = b.payment_mode.value.title() if b.payment_mode else "Unknown"
                if b.payment_mode and b.payment_mode.value != "free":
                    mt[lbl] += _f(b.total)
                mc[lbl] += 1

            mo_modes = [
                {"label": lbl, "amount": amt, "count": mc[lbl],
                 "pct": round(amt / mo_total * 100) if mo_total > 0 else 0}
                for lbl, amt in sorted(mt.items(), key=lambda x: -x[1])
            ]

            year_grand_total += mo_total
            year_txn_count   += mo_count
            yearly_months.append({
                "month":      mo,
                "month_name": _cal.month_abbr[mo],
                "total":      mo_total,
                "count":      mo_count,
                "modes":      mo_modes,
                "has_data":   mo_count > 0,
            })

        return templates.TemplateResponse(request, "income_transactions.html", {
            "active":           "income",
            "doctor":           doctor,
            "pin_required":     getattr(request.state, "pin_required", False),
            "view":             "yearly",
            "year":             year,
            "month":            month,
            "year_list":        year_list,
            "prev_year":        year - 1,
            "next_year":        year + 1,
            "is_current_year":  year == today.year,
            "yearly_months":    yearly_months,
            "year_grand_total": year_grand_total,
            "year_txn_count":   year_txn_count,
            # monthly view stubs (not used but avoids template errors)
            "bills": [], "total_txns": 0, "total_pages": 1, "page": 1,
            "page_start": 0, "page_end": 0, "per_page": TXN_PER_PAGE,
            "month_total": 0, "mode_breakdown": [], "month_name": "",
            "month_list": [], "months_by_year": {},
        })

    # ────────────────────────────────────────────────────────────────────────
    #  MONTHLY VIEW
    # ────────────────────────────────────────────────────────────────────────
    m_first, m_last = _month_range(year, month)

    base = (
        db.query(Bill)
        .filter(
            Bill.doctor_id == doctor.id,
            Bill.clinic_id == _acid_scope,
            Bill.paid_at   >= _dt_start(m_first),
            Bill.paid_at   <= _dt_end(m_last),
        )
        .order_by(Bill.paid_at.desc().nullslast(), Bill.created_at.desc())
    )

    total_txns  = base.count()
    total_pages = max(1, _math.ceil(total_txns / TXN_PER_PAGE))
    page        = max(1, min(page, total_pages))
    offset      = (page - 1) * TXN_PER_PAGE

    bills = base.offset(offset).limit(TXN_PER_PAGE).all()
    for b in bills:
        if b.visit:
            _ = b.visit.patient

    page_start = offset + 1 if total_txns > 0 else 0
    page_end   = min(offset + TXN_PER_PAGE, total_txns)

    all_month_bills = base.all()
    month_total = sum(_f(b.total) for b in all_month_bills if b.payment_mode and b.payment_mode.value != "free")

    mode_totals: dict[str, float] = defaultdict(float)
    mode_counts: dict[str, int]   = defaultdict(int)
    for b in all_month_bills:
        label = b.payment_mode.value.title() if b.payment_mode else "Unknown"
        if b.payment_mode and b.payment_mode.value == "free":
            mode_counts[label] += 1
        else:
            mode_totals[label] += _f(b.total)
            mode_counts[label] += 1

    mode_breakdown = [
        {"label": lbl, "amount": amt, "count": mode_counts[lbl],
         "pct": round(amt / month_total * 100) if month_total > 0 else 0}
        for lbl, amt in sorted(mode_totals.items(), key=lambda x: -x[1])
    ]
    if mode_counts.get("Free"):
        mode_breakdown.append({"label": "Free", "amount": 0, "count": mode_counts["Free"], "pct": 0})

    return templates.TemplateResponse(request, "income_transactions.html", {
        "active":         "income",
        "doctor":         doctor,
        "pin_required":   getattr(request.state, "pin_required", False),
        "view":           "monthly",
        "bills":          bills,
        "month":          month,
        "year":           year,
        "month_name":     _cal.month_name[month],
        "month_list":     sorted(month_set, reverse=True),
        "months_by_year": dict(months_by_year),
        "year_list":      year_list,
        "total_txns":     total_txns,
        "total_pages":    total_pages,
        "page":           page,
        "page_start":     page_start,
        "page_end":       page_end,
        "per_page":       TXN_PER_PAGE,
        "month_total":    month_total,
        "mode_breakdown": mode_breakdown,
        # yearly stubs
        "yearly_months": [], "year_grand_total": 0, "year_txn_count": 0,
        "prev_year": year - 1, "next_year": year + 1,
        "is_current_year": year == today.year,
    })


# ═══════════════════════════════════════════════════════════════════════════════
#  GET /expenses  — tracker page
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/expenses", response_class=HTMLResponse)
async def expenses_page(
    request: Request,
    month:   int = 0,
    year:    int = 0,
    db:      Session = Depends(get_db),
    doctor:  Doctor  = Depends(get_paying_doctor),
):
    # Money is attributed to the clinic the work happened at.
    _acid_scope = getattr(request.state, "active_clinic_id", None)
    _fire_due_recurring(doctor.id, db)

    _acid2 = getattr(request.state, "active_clinic_id", None)

    today = date.today()
    if not month: month = today.month
    if not year:  year  = today.year

    m_first, m_last = _month_range(year, month)
    prev_d = m_first - timedelta(days=1)
    next_d = m_last  + timedelta(days=1)

    expenses = (
        db.query(Expense)
        .filter(
            Expense.doctor_id    == doctor.id,
            Expense.clinic_id == _acid_scope,
            Expense.expense_date >= m_first,
            Expense.expense_date <= m_last,
        )
        .order_by(Expense.expense_date.desc(), Expense.created_at.desc())
        .all()
    )

    month_expense = sum(_f(e.amount) for e in expenses)

    # Income this month (for inline P&L)
    month_income = _income_sum(doctor.id, m_first, m_last, db, _acid2)
    pnl          = month_income - month_expense

    # Year totals
    year_expense = _expense_sum(doctor.id, date(year,1,1), date(year,12,31), db, _acid2)
    year_income  = _income_sum (doctor.id, date(year,1,1), date(year,12,31), db, _acid2)

    # Category totals for the bar breakdown
    cat_totals: dict[str, float] = defaultdict(float)
    for e in expenses:
        cat_totals[e.category.value] += _f(e.amount)

    recurring_rules = (
        db.query(RecurringExpense)
        .filter(RecurringExpense.doctor_id == doctor.id)
        .order_by(RecurringExpense.is_active.desc(), RecurringExpense.created_at)
        .all()
    )

    month_names = [
        "January","February","March","April","May","June",
        "July","August","September","October","November","December",
    ]

    return templates.TemplateResponse(request, "expenses.html", {
        "active":           "expenses",
        "doctor":           doctor,
        "today":            today,
        "sel_month":        month,
        "sel_year":         year,
        "sel_month_name":   month_names[month - 1],
        "prev_month":       prev_d.month,
        "prev_year":        prev_d.year,
        "next_month":       next_d.month,
        "next_year":        next_d.year,
        "is_current_month": (month == today.month and year == today.year),
        "expenses":         expenses,
        "month_expense":    month_expense,
        "month_income":     month_income,
        "year_expense":     year_expense,
        "year_income":      year_income,
        "pnl":              pnl,
        "cat_totals":       dict(cat_totals),
        "ExpenseCategory":  ExpenseCategory,
        "recurring_rules":  recurring_rules,
        "month_names":      month_names,
    })


# ═══════════════════════════════════════════════════════════════════════════════
#  POST /expenses  — add one-off expense
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/expenses")
async def add_expense(
    request: Request,
    db:      Session = Depends(get_db),
    doctor:  Doctor  = Depends(get_paying_doctor),
):
    form = await request.form()

    try:
        amount = float(form.get("amount") or 0)
    except ValueError:
        amount = 0.0

    try:
        category = ExpenseCategory(form.get("category", "misc"))
    except ValueError:
        category = ExpenseCategory.misc

    description = (form.get("description") or "").strip() or None

    try:
        expense_date = date.fromisoformat(str(form.get("expense_date", "")))
    except ValueError:
        expense_date = date.today()

    if amount > 0:
        db.add(Expense(
            doctor_id    = doctor.id,
            category     = category,
            amount       = amount,
            expense_date = expense_date,
            description  = description,
        ))
        db.commit()

    return RedirectResponse(
        f"/expenses?month={expense_date.month}&year={expense_date.year}",
        status_code=303,
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  POST /expenses/{id}/delete
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/expenses/{expense_id}/delete")
async def delete_expense(
    expense_id: int,
    request:    Request,
    db:         Session = Depends(get_db),
    doctor:     Doctor  = Depends(get_paying_doctor),
):
    # Money is attributed to the clinic the work happened at.
    _acid_scope = getattr(request.state, "active_clinic_id", None)
    exp = db.query(Expense).filter(
        Expense.id        == expense_id,
        Expense.doctor_id == doctor.id,
        Expense.clinic_id == _acid_scope,
    ).first()
    redirect_month = date.today().month
    redirect_year  = date.today().year
    if exp:
        redirect_month = exp.expense_date.month
        redirect_year  = exp.expense_date.year
        db.delete(exp)
        db.commit()
    return RedirectResponse(
        f"/expenses?month={redirect_month}&year={redirect_year}",
        status_code=303,
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  POST /expenses/recurring  — add rule
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/expenses/recurring")
async def add_recurring(
    request: Request,
    db:      Session = Depends(get_db),
    doctor:  Doctor  = Depends(get_paying_doctor),
):
    form = await request.form()

    try:
        amount = float(form.get("amount") or 0)
    except ValueError:
        amount = 0.0

    try:
        category = ExpenseCategory(form.get("category", "misc"))
    except ValueError:
        category = ExpenseCategory.misc

    label = (form.get("label") or "").strip()

    try:
        day_of_month = max(1, min(28, int(form.get("day_of_month") or 1)))
    except ValueError:
        day_of_month = 1

    if amount > 0 and label:
        db.add(RecurringExpense(
            doctor_id    = doctor.id,
            category     = category,
            amount       = amount,
            label        = label,
            day_of_month = day_of_month,
            is_active    = True,
        ))
        db.commit()

    return RedirectResponse("/expenses", status_code=303)


# ═══════════════════════════════════════════════════════════════════════════════
#  POST /expenses/recurring/{id}/toggle
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/expenses/recurring/{rule_id}/toggle")
async def toggle_recurring(
    rule_id: int,
    request: Request,
    db:      Session = Depends(get_db),
    doctor:  Doctor  = Depends(get_paying_doctor),
):
    rule = db.query(RecurringExpense).filter(
        RecurringExpense.id        == rule_id,
        RecurringExpense.doctor_id == doctor.id,
    ).first()
    if rule:
        rule.is_active = not rule.is_active
        db.commit()
    return RedirectResponse("/expenses", status_code=303)


# ═══════════════════════════════════════════════════════════════════════════════
#  POST /expenses/recurring/{id}/delete
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/expenses/recurring/{rule_id}/delete")
async def delete_recurring(
    rule_id: int,
    request: Request,
    db:      Session = Depends(get_db),
    doctor:  Doctor  = Depends(get_paying_doctor),
):
    rule = db.query(RecurringExpense).filter(
        RecurringExpense.id        == rule_id,
        RecurringExpense.doctor_id == doctor.id,
    ).first()
    if rule:
        # Detach child expense rows so deleting the rule doesn't cascade them
        db.query(Expense).filter(Expense.recurring_id == rule_id).update(
            {"recurring_id": None}
        )
        db.delete(rule)
        db.commit()
    return RedirectResponse("/expenses", status_code=303)

