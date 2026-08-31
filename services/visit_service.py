"""
visit_service.py — Token assignment, queue management, and state machine for v2.

Core concepts:
  - Visit  : one row per patient per day (primary queue entity)
  - token_number   : monotonic, never changes, patient-visible (printed on slip)
  - queue_position : mutable, used for reordering; lower = called sooner
  - Walk-in policy : 'booked_jumps' | 'fcfs' | 'ask'
"""

from __future__ import annotations

from datetime import datetime, date, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from database.models import (
    Visit, VisitStatus, VisitSource,
    Appointment, AppointmentStatus,
    Patient, Doctor,
)


# --------------------------------------------------------------------------- #
#  Token assignment                                                             #
# --------------------------------------------------------------------------- #

def _clinic_filter(q, clinic_id):
    """Restrict a queue query to one clinic.

    Tokens and queue positions are per (doctor, clinic, day). Without this, a
    doctor running a morning shift at their own clinic and an evening shift
    elsewhere shared ONE token sequence — and promoting an emergency at one
    clinic renumbered the other clinic's live queue.
    """
    return q.filter(Visit.clinic_id == clinic_id) if clinic_id is not None else q


def _next_token_number(db: Session, doctor_id: int, visit_date: date,
                       clinic_id: Optional[int] = None) -> int:
    """Return the next monotonic token number for this doctor+clinic today."""
    from sqlalchemy import func
    result = _clinic_filter(
        db.query(func.max(Visit.token_number))
        .filter(Visit.doctor_id == doctor_id, Visit.visit_date == visit_date),
        clinic_id,
    ).scalar()
    return (result or 0) + 1


def _queue_end_position(db: Session, doctor_id: int, visit_date: date,
                        clinic_id: Optional[int] = None) -> int:
    """Return a queue_position one after the current last waiting/serving visit."""
    from sqlalchemy import func
    result = _clinic_filter(
        db.query(func.max(Visit.queue_position))
        .filter(
            Visit.doctor_id == doctor_id,
            Visit.visit_date == visit_date,
            Visit.status.in_([VisitStatus.waiting, VisitStatus.serving]),
        ),
        clinic_id,
    ).scalar()
    return (result or 0) + 1


def check_in(
    db: Session,
    *,
    doctor_id: int,
    patient_id: int,
    clinic_id: Optional[int] = None,
    appointment_id: Optional[int] = None,
    is_emergency: bool = False,
    notes: Optional[str] = None,
    created_by: Optional[int] = None,
    visit_date: Optional[date] = None,
) -> Visit:
    """
    Create a Visit for a patient checking in today.

    Walk-in policy (from doctor.walkin_policy):
      booked_jumps — a booked patient arriving around their slot gets slotted
                     in by slot order; walk-ins always go to the end.
      fcfs         — queue_position = end for everyone (pure first-come-first-served)
      ask          — always queue_position = end; UI will prompt the receptionist
                     to manually reorder if needed (not enforced in service layer)
    """
    today = visit_date or date.today()
    now   = datetime.now()

    doctor = db.query(Doctor).filter(Doctor.id == doctor_id).first()
    policy = (doctor.walkin_policy or "booked_jumps") if doctor else "booked_jumps"

    token_number = _next_token_number(db, doctor_id, today, clinic_id)

    # --- Determine queue_position ---
    if is_emergency:
        # Emergency: insert at position 0 (pushes everyone else down by 1)
        _shift_queue_down(db, doctor_id, today, from_position=0, clinic_id=clinic_id)
        queue_position = 0
        source = VisitSource.appointment if appointment_id else VisitSource.walk_in
    elif appointment_id and policy == "booked_jumps":
        appt = db.query(Appointment).filter(Appointment.id == appointment_id).first()
        queue_position = _compute_booked_position(db, doctor_id, today, appt, now, clinic_id)
        source = VisitSource.appointment
    else:
        # Walk-in or fcfs/ask policy
        queue_position = _queue_end_position(db, doctor_id, today, clinic_id)
        source = VisitSource.appointment if appointment_id else VisitSource.walk_in

    visit = Visit(
        doctor_id      = doctor_id,
        patient_id     = patient_id,
        clinic_id      = clinic_id,
        appointment_id = appointment_id,
        visit_date     = today,
        token_number   = token_number,
        queue_position = queue_position,
        status         = VisitStatus.waiting,
        is_emergency   = is_emergency,
        source         = source,
        check_in_time  = now,
        notes          = notes,
        created_by     = created_by,
    )
    db.add(visit)

    # Mark appointment as arrived
    if appointment_id:
        appt = db.query(Appointment).filter(Appointment.id == appointment_id).first()
        if appt:
            appt.arrival_status = "arrived"
            appt.visit_id = None  # will set after flush
            db.flush()
            appt.visit_id = visit.id

    db.commit()
    db.refresh(visit)
    return visit


def _compute_booked_position(
    db: Session, doctor_id: int, today: date,
    appt: Optional[Appointment], now: datetime,
    clinic_id: Optional[int] = None
) -> int:
    """
    Slot a booked patient into the queue based on drift from scheduled time.

    Drift rules (matching architecture doc §4.2):
      < -30 min (very early)     → end of queue
      -30..+30 min (around slot) → slot-ordered position
      +30..+120 min (late)       → end of queue, flagged
      > +120 min (very late)     → end of queue (receptionist shown alert)
    """
    if not appt:
        return _queue_end_position(db, doctor_id, today, clinic_id)

    slot_dt  = datetime.combine(today, appt.appointment_time)
    drift    = (now - slot_dt).total_seconds() / 60  # minutes; positive = late

    if drift < -30 or drift > 30:
        return _queue_end_position(db, doctor_id, today, clinic_id)

    # Around the slot — find position by slot time ordering
    # Count how many waiting/serving visits have appointment slots BEFORE this one
    earlier = _clinic_filter(
        db.query(Visit)
        .join(Appointment, Visit.appointment_id == Appointment.id, isouter=True)
        .filter(
            Visit.doctor_id == doctor_id,
            Visit.visit_date == today,
            Visit.status.in_([VisitStatus.waiting, VisitStatus.serving]),
            Appointment.appointment_time < appt.appointment_time,
        ),
        clinic_id,
    ).count()
    # Insert after earlier booked visits, bump the rest
    position = earlier + 1
    _shift_queue_down(db, doctor_id, today, from_position=position, clinic_id=clinic_id)
    return position


def _shift_queue_down(db: Session, doctor_id: int, today: date, from_position: int,
                      clinic_id: Optional[int] = None):
    """Increment queue_position for all waiting visits at or after from_position."""
    visits = _clinic_filter(
        db.query(Visit)
        .filter(
            Visit.doctor_id == doctor_id,
            Visit.visit_date == today,
            Visit.status.in_([VisitStatus.waiting, VisitStatus.serving]),
            Visit.queue_position >= from_position,
        ),
        clinic_id,
    ).all()
    for v in visits:
        v.queue_position += 1


# --------------------------------------------------------------------------- #
#  State transitions                                                            #
# --------------------------------------------------------------------------- #

def call_next(db: Session, doctor_id: int, visit_date: Optional[date] = None,
              clinic_id: Optional[int] = None) -> Optional[Visit]:
    """
    Call the next waiting patient:
      1. The currently SERVING visit (if any) is NOT auto-closed — caller handles that.
      2. Find the WAITING visit with the lowest queue_position.
      3. Move it to SERVING, set call_time.
    Returns the newly-serving Visit, or None if queue is empty.
    """
    today = visit_date or date.today()
    nxt = _clinic_filter(
        db.query(Visit)
        .filter(
            Visit.doctor_id == doctor_id,
            Visit.visit_date == today,
            Visit.status == VisitStatus.waiting,
        ),
        clinic_id,
    ).order_by(Visit.is_emergency.desc(), Visit.queue_position.asc()).first()
    if not nxt:
        return None

    nxt.status    = VisitStatus.serving
    nxt.call_time = datetime.now()
    db.commit()
    db.refresh(nxt)
    return nxt


def done_and_call_next(
    db: Session,
    visit: Visit,
) -> Optional[Visit]:
    """
    Mark current visit as BILLING_PENDING (bill modal will close it to DONE).
    Then auto-call the next waiting patient.
    Returns the newly-serving Visit or None.
    """
    visit.status = VisitStatus.billing_pending
    db.commit()
    return call_next(db, visit.doctor_id, visit.visit_date, visit.clinic_id)


def close_visit(db: Session, visit: Visit, bill_id: int):
    """Called after a Bill is saved — moves visit to DONE."""
    visit.status        = VisitStatus.done
    visit.complete_time = datetime.now()
    visit.bill_id       = bill_id
    db.commit()


def hold_visit(db: Session, visit: Visit) -> Optional[Visit]:
    """
    Put the currently-serving patient ON HOLD (e.g. sent for x-ray / lab work)
    and auto-call the next waiting patient so the doctor keeps moving.
    The held patient can be brought back later with resume_visit().
    Returns the newly-serving Visit or None.
    """
    visit.status = VisitStatus.on_hold
    db.commit()
    return call_next(db, visit.doctor_id, visit.visit_date, visit.clinic_id)


def resume_visit(db: Session, visit: Visit) -> Visit:
    """
    Bring an ON HOLD patient back.
      • If the doctor is free (no one serving) → serve them immediately.
      • If someone is currently serving → put them at the FRONT of the queue
        (next up) so they are seen right after the current patient.
    """
    serving = (
        db.query(Visit)
        .filter(
            Visit.doctor_id  == visit.doctor_id,
            Visit.visit_date == visit.visit_date,
            Visit.status     == VisitStatus.serving,
        )
        .first()
    )
    if serving:
        _shift_queue_down(db, visit.doctor_id, visit.visit_date, from_position=0, clinic_id=visit.clinic_id)
        visit.queue_position = 0
        visit.status         = VisitStatus.waiting
    else:
        visit.status    = VisitStatus.serving
        visit.call_time = datetime.now()
    db.commit()
    db.refresh(visit)
    return visit


def skip_visit(db: Session, visit: Visit) -> Visit:
    """Skip a waiting visit — move it to the end of the queue."""
    today = visit.visit_date
    end   = _queue_end_position(db, visit.doctor_id, today, visit.clinic_id)
    visit.queue_position = end
    visit.status         = VisitStatus.skipped
    db.commit()
    db.refresh(visit)
    return visit


def promote_emergency(db: Session, visit: Visit) -> Visit:
    """Promote any waiting visit to emergency — insert at top of queue."""
    _shift_queue_down(db, visit.doctor_id, visit.visit_date, from_position=0, clinic_id=visit.clinic_id)
    visit.queue_position = 0
    visit.is_emergency   = True
    db.commit()
    db.refresh(visit)
    return visit


def cancel_visit(db: Session, visit: Visit) -> Visit:
    visit.status = VisitStatus.cancelled
    db.commit()
    db.refresh(visit)
    return visit


def move_visit(db: Session, visit: Visit, new_position: int) -> Visit:
    """Manually reorder a visit in the queue (drag-and-drop support).

    Rewritten — the previous version had three defects:

      * it renumbered the losing branch from ZERO (`v.queue_position = i`)
        while every other queue path is 1-based, so a drag to the front left
        the displaced patient at position 0;
      * it was scoped to doctor + date only, so a doctor who works at two
        clinics had the OTHER clinic's queue renumbered by a drag in this one
        — the same defect already fixed for emergency promotion;
      * new_position came straight off a form with no bounds check, so a
        hand-posted value produced arbitrary positions.

    It also built a `positions` list that was never read.

    The queue is rebuilt as a plain ordered list: pull the visit out, insert it
    at the clamped index, then number 1..N. That is inherently contiguous and
    cannot drift.
    """
    today = visit.visit_date

    others = (
        db.query(Visit)
        .filter(
            Visit.doctor_id == visit.doctor_id,
            Visit.clinic_id == visit.clinic_id,   # one clinic's queue only
            Visit.visit_date == today,
            Visit.status == VisitStatus.waiting,
            Visit.id != visit.id,
        )
        .order_by(Visit.queue_position.asc())
        .all()
    )

    # 1-based target, clamped to the queue that actually exists.
    target = max(1, min(int(new_position or 1), len(others) + 1))

    ordered = others[:target - 1] + [visit] + others[target - 1:]
    for i, v in enumerate(ordered, start=1):
        v.queue_position = i

    db.commit()
    db.refresh(visit)
    return visit


# --------------------------------------------------------------------------- #
#  Queue queries                                                                #
# --------------------------------------------------------------------------- #

def get_today_visits(db: Session, doctor_id: int, visit_date: Optional[date] = None):
    """
    Returns (serving, waiting_list, closed_list) for the given date.
    serving      — single Visit or None
    waiting_list — ordered by is_emergency desc, queue_position asc
    closed_list  — ordered by complete_time desc (done + cancelled + no_show)
    """
    today = visit_date or date.today()

    serving = (
        db.query(Visit)
        .filter(Visit.doctor_id == doctor_id, Visit.visit_date == today,
                Visit.status == VisitStatus.serving)
        .first()
    )
    # Also include billing_pending in "serving" display area
    if not serving:
        serving = (
            db.query(Visit)
            .filter(Visit.doctor_id == doctor_id, Visit.visit_date == today,
                    Visit.status == VisitStatus.billing_pending)
            .first()
        )

    waiting = (
        db.query(Visit)
        .filter(Visit.doctor_id == doctor_id, Visit.visit_date == today,
                Visit.status == VisitStatus.waiting)
        .order_by(Visit.is_emergency.desc(), Visit.queue_position.asc())
        .all()
    )

    closed = (
        db.query(Visit)
        .filter(
            Visit.doctor_id == doctor_id,
            Visit.visit_date == today,
            Visit.status.in_([
                VisitStatus.done, VisitStatus.cancelled,
                VisitStatus.no_show, VisitStatus.skipped,
            ]),
        )
        .order_by(Visit.complete_time.desc())
        .all()
    )

    return serving, waiting, closed


def get_queue_status_json(db: Session, doctor_id: int) -> dict:
    """
    Compact JSON for the public display screen (/queue/{slug}) polling.
    """
    today = date.today()
    serving, waiting, _ = get_today_visits(db, doctor_id)

    now_serving = None
    if serving:
        now_serving = {
            "token":  serving.token_number,
            "name":   serving.patient.name if serving.patient else "",
            "is_emergency": serving.is_emergency,
        }

    up_next = [
        {"token": v.token_number, "is_emergency": v.is_emergency}
        for v in waiting[:4]
    ]

    # Simple avg wait: 10 min default (we don't track real averages yet)
    avg_wait = max(5, len(waiting) * 10)

    return {
        "now_serving":   now_serving,
        "up_next":       up_next,
        "queue_length":  len(waiting),
        "avg_wait_mins": avg_wait,
    }


# --------------------------------------------------------------------------- #
#  Auto no-show mark (called by scheduler)                                     #
# --------------------------------------------------------------------------- #

def auto_mark_no_shows(db: Session):
    """
    Mark appointments as no_show if:
      - they are still 'booked' (arrival_status is NULL or 'booked')
      - their scheduled time was > 2 hours ago
      - they have no associated Visit
    Safe to call repeatedly.
    """
    cutoff = datetime.now() - timedelta(hours=2)
    today  = date.today()

    stale_appts = (
        db.query(Appointment)
        .filter(
            Appointment.appointment_date == today,
            Appointment.visit_id.is_(None),
            Appointment.arrival_status.in_([None, "booked"]),
            Appointment.status == AppointmentStatus.scheduled,
        )
        .all()
    )
    for appt in stale_appts:
        slot_dt = datetime.combine(today, appt.appointment_time)
        if slot_dt < cutoff:
            appt.arrival_status = "no_show"
            appt.status = AppointmentStatus.no_show

    if stale_appts:
        db.commit()
