"""
Notification service — Twilio WhatsApp sending (sandbox for testing).

Twilio sandbox setup:
  1. Patient sends "join <keyword>" to whatsapp:+14155238886 once to opt in
  2. All messages then go through fine for testing
"""
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from config import settings
from database.models import (
    Appointment, NotificationLog, NotificationChannel, NotificationType,
)

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
#  Phone formatting                                                    #
# ------------------------------------------------------------------ #

def _e164(phone: str) -> str:
    phone = phone.strip().replace(" ", "").replace("-", "")
    if phone.startswith("+"):
        return phone
    if phone.startswith("91") and len(phone) == 12:
        return f"+{phone}"
    if len(phone) == 10:
        return f"+91{phone}"
    return f"+{phone}"


# ------------------------------------------------------------------ #
#  Low-level sender (Twilio sandbox)                                   #
# ------------------------------------------------------------------ #

def _twilio_client():
    from twilio.rest import Client
    return Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)


def send_whatsapp(to_phone: str, message: str) -> tuple[bool, str]:
    if not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN:
        return False, "Twilio not configured"
    try:
        client = _twilio_client()
        msg = client.messages.create(
            from_=settings.TWILIO_WHATSAPP_FROM,
            to=f"whatsapp:{_e164(to_phone)}",
            body=message,
        )
        return True, msg.sid
    except Exception as e:
        logger.error(f"Twilio WhatsApp error: {e}")
        return False, str(e)


# ------------------------------------------------------------------ #
#  Send with fallback (WhatsApp only for now)                          #
# ------------------------------------------------------------------ #

def _send_with_fallback(
    phone: str, message: str
) -> tuple[bool, NotificationChannel, str]:
    ok, sid = send_whatsapp(phone, message)
    return ok, NotificationChannel.whatsapp, sid



# ------------------------------------------------------------------ #
#  Clinic identity for patient-facing messages                         #
# ------------------------------------------------------------------ #

def _clinic_identity(doctor, db=None, clinic_id=None):
    """(name, address) to show the patient.

    Doctor.clinic_name/clinic_address are per-doctor free text. Reading them
    directly meant a doctor working a shift at someone else's clinic sent the
    patient THEIR OWN clinic name — and an invite-registered associate has
    those fields NULL by construction, so patients were told to attend
    "Dr. Priya Sharma's clinic" with no address at all, while physically
    booked into a real clinic with a real address.

    Resolve from the Clinic row the work is attributed to; fall back to the
    doctor's own fields only when there is no clinic (solo, pre-backfill).
    """
    if db is not None and clinic_id:
        try:
            from database.models import Clinic
            c = db.query(Clinic).filter(Clinic.id == clinic_id).first()
            if c and c.name:
                city = getattr(c, "city", None)
                addr = getattr(c, "address", None)
                full = ", ".join(x for x in (addr, city) if x)
                if full:
                    return c.name, full
                # Clinic named but not addressed — better than the doctor's
                # unrelated personal address.
                return c.name, None
        except Exception:
            pass
    name = doctor.clinic_name or f"Dr. {doctor.name}'s clinic"
    addr = None
    if doctor.clinic_address:
        addr = f"{doctor.clinic_address}, {doctor.city or ''}".rstrip(", ")
    return name, addr


# ------------------------------------------------------------------ #
#  Message builders                                                    #
# ------------------------------------------------------------------ #

def _confirmation_msg(appt: Appointment, doctor, db=None) -> str:
    clinic_name, clinic_addr = _clinic_identity(
        doctor, db, getattr(appt, "clinic_id", None))
    date_str = appt.appointment_date.strftime("%-d %b %Y")
    t = appt.appointment_time
    hour = t.hour % 12 or 12
    ampm = "AM" if t.hour < 12 else "PM"
    time_str = f"{hour}:{t.minute:02d} {ampm}"

    lines = [
        f"Hello {appt.patient.name},\n",
        f"Your appointment at *{clinic_name}* is confirmed.",
        f"Date: *{date_str}*  Time: *{time_str}*",
        f"Duration: {appt.duration_mins} mins",
    ]
    if clinic_addr:
        lines.append(f"Address: {clinic_addr}")
    lines.append("\nPlease arrive 5 minutes early. To reschedule, call the clinic directly.")
    return "\n".join(lines)


def _reminder_msg(appt: Appointment, doctor, reminder_type: str, db=None) -> str:
    clinic, _ = _clinic_identity(doctor, db, getattr(appt, "clinic_id", None))
    t        = appt.appointment_time
    hour     = t.hour % 12 or 12
    ampm     = "AM" if t.hour < 12 else "PM"
    time_str = f"{hour}:{t.minute:02d} {ampm}"
    date_str = appt.appointment_date.strftime("%-d %b %Y")

    if reminder_type == "24h":
        return (
            f"Reminder: You have an appointment at *{clinic}* "
            f"tomorrow (*{date_str}*) at *{time_str}*.\n\n"
            f"See you then!"
        )
    # 2h
    return (
        f"Reminder: Your appointment at *{clinic}* "
        f"is in about 2 hours, at *{time_str}* today.\n\n"
        f"See you soon!"
    )


# ------------------------------------------------------------------ #
#  DB log helper                                                       #
# ------------------------------------------------------------------ #

def _log(
    appt_id,       # int or None — nullable since bill/walk-in logs have no appointment
    notif_type: NotificationType,
    channel: NotificationChannel,
    message: str,
    status: str,
    db: Session,
):
    entry = NotificationLog(
        appointment_id=appt_id,
        type=notif_type,
        channel=channel,
        message_body=message,
        status=status,
        sent_at=datetime.utcnow() if status == "sent" else None,
    )
    db.add(entry)
    db.commit()


# ------------------------------------------------------------------ #
#  High-level triggers (called from routers / scheduler)              #
# ------------------------------------------------------------------ #

def notify_appointment_confirmed(appt: Appointment, doctor, db: Session):
    """Send booking confirmation via WhatsApp.

    Called immediately after an appointment is created (by doctor or patient).
    Failure is logged but never raises — the booking always succeeds.
    """
    _ = appt.patient  # ensure lazy-loaded
    message = _confirmation_msg(appt, doctor, db)

    success, channel, result = _send_with_fallback(appt.patient.phone, message)
    status = "sent" if success else "failed"
    _log(appt.id, NotificationType.confirmation, channel, message, status, db)

    if success:
        logger.info(f"Confirmation sent ({channel.value}) for appt #{appt.id} id={result}")
    else:
        logger.warning(f"Confirmation failed for appt #{appt.id}: {result}")


def notify_followup_confirmed(appt: Appointment, doctor, db: Session):
    """Send follow-up appointment confirmation via WhatsApp."""
    if not appt.patient or not appt.patient.phone:
        return
    clinic_name = doctor.clinic_name or f"Dr. {doctor.name}'s clinic"
    date_str = appt.appointment_date.strftime("%-d %b %Y")
    t = appt.appointment_time
    hour = t.hour % 12 or 12
    ampm = "AM" if t.hour < 12 else "PM"
    time_str = f"{hour}:{t.minute:02d} {ampm}"

    message = (
        f"Hello {appt.patient.name},\n\n"
        f"Your follow-up appointment at *{clinic_name}* is confirmed.\n"
        f"Date: *{date_str}*  Time: *{time_str}*\n"
        f"Duration: {appt.duration_mins} mins\n\n"
        f"Please bring your previous reports and prescriptions.\n"
        f"To reschedule, call the clinic directly."
    )
    ok, channel, sid = _send_with_fallback(appt.patient.phone, message)
    _log(appt.id, NotificationType.confirmation, channel, message, "sent" if ok else "failed", db)


def notify_reminder(appt: Appointment, doctor, db: Session, reminder_type: str):
    """Send a reminder via WhatsApp.

    Called by the background scheduler.
    """
    _ = appt.patient  # ensure lazy-loaded
    message = _reminder_msg(appt, doctor, reminder_type, db)

    success, channel, result = _send_with_fallback(appt.patient.phone, message)
    notif_type = (
        NotificationType.reminder_24h if reminder_type == "24h"
        else NotificationType.reminder_2h
    )
    status = "sent" if success else "failed"
    _log(appt.id, notif_type, channel, message, status, db)

    if success:
        logger.info(f"{reminder_type} reminder sent ({channel.value}) for appt #{appt.id} id={result}")
    else:
        logger.warning(f"{reminder_type} reminder failed for appt #{appt.id}: {result}")


def notify_walkin_queued(visit, doctor, db: Session):
    """Send queue position + estimated wait to a walk-in patient."""
    patient = visit.patient
    if not patient or not patient.phone:
        return
    clinic_name = doctor.clinic_name or f"Dr. {doctor.name}'s clinic"

    # Count WAITING visits ahead in queue
    from database.models import Visit as VisitModel, VisitStatus
    people_ahead = db.query(VisitModel).filter(
        VisitModel.doctor_id == doctor.id,
        VisitModel.visit_date == visit.visit_date,
        VisitModel.status == VisitStatus.waiting,
        VisitModel.queue_position < visit.queue_position,
    ).count()

    avg_mins = doctor.avg_consult_mins or 10
    estimated_wait = people_ahead * avg_mins

    message = (
        f"Hello {patient.name},\n\n"
        f"You are checked in at *{clinic_name}*.\n"
        f"Token: *#{visit.token_number}*\n"
        f"People ahead of you: *{people_ahead}*\n"
        f"Estimated wait: *~{estimated_wait} mins*\n\n"
        f"We will call you shortly."
    )
    ok, channel, sid = _send_with_fallback(patient.phone, message)
    _log(visit.appointment_id, NotificationType.walkin_queue, channel, message, "sent" if ok else "failed", db)


def notify_bill_receipt(bill, doctor, db: Session):
    """Send a text bill receipt to the patient via WhatsApp."""
    from database.models import Patient
    patient = db.query(Patient).filter(Patient.id == bill.patient_id).first()
    if not patient or not patient.phone:
        return
    clinic_name = doctor.clinic_name or f"Dr. {doctor.name}'s clinic"

    # All line items
    items = list(bill.items) if bill.items else []

    # Payment mode label
    mode_labels = {
        "cash":      "Cash",
        "upi":       "UPI",
        "card":      "Card",
        "insurance": "Insurance",
        "free":      "Free",
        "partial":   "Partial",
    }
    mode_label = mode_labels.get(
        bill.payment_mode.value if bill.payment_mode else "cash", "Cash"
    )

    # Build itemised block
    if items:
        item_lines = []
        for i in items:
            qty_str = f" x{int(i.quantity)}" if i.quantity and int(i.quantity) > 1 else ""
            item_lines.append(f"• {i.description}{qty_str}: ₹{i.total:.0f}")
        items_block = "\n".join(item_lines)
    else:
        items_block = f"• Consultation: ₹{bill.subtotal:.0f}"

    # Totals — only show discount/GST lines if non-zero
    totals_lines = []
    if bill.discount and float(bill.discount) > 0:
        totals_lines.append(f"• Subtotal: ₹{bill.subtotal:.0f}")
        totals_lines.append(f"• Discount: -₹{bill.discount:.0f}")
    if bill.gst_amount and float(bill.gst_amount) > 0:
        totals_lines.append(f"• GST: ₹{bill.gst_amount:.0f}")
    totals_block = "\n".join(totals_lines)

    visit_date = bill.visit.visit_date.strftime("%-d %b %Y") if bill.visit else datetime.now().strftime("%-d %b %Y")

    # Merge items + subtotal/discount/GST into one bullet block
    full_bullets = [items_block]
    if totals_block:
        full_bullets.append(totals_block)

    # Create a feedback link (separate from the billing content, not merged with
    # any prescription message) so the patient can rate the clinic.
    feedback_url = _create_feedback_link(bill, doctor, patient, db)

    message_parts = [
        f"*Bill Receipt — {clinic_name}*",
        f"Date: {visit_date}",
        f"Patient: {patient.name}",
        "",
        "\n".join(full_bullets),
        "",
        f"*Total: ₹{bill.total:.0f}*",
        f"Paid via: {mode_label}",
        "",
        f"Thank you for visiting {clinic_name}.",
    ]
    if feedback_url:
        message_parts += [
            "",
            "We would love your feedback — rate your visit here:",
            feedback_url,
        ]
    message = "\n".join(message_parts)
    appt_id = bill.visit.appointment_id if bill.visit else None
    ok, channel, sid = _send_with_fallback(patient.phone, message)
    _log(appt_id, NotificationType.bill_receipt, channel, message, "sent" if ok else "failed", db)


def _create_feedback_link(bill, doctor, patient, db: Session) -> str:
    """Create (or reuse) a Feedback row for this bill and return its public URL.
    Never raises — a link failure must not break the receipt send."""
    import secrets
    from database.models import Feedback
    try:
        existing = db.query(Feedback).filter(Feedback.bill_id == bill.id).first()
        if existing:
            fb = existing
        else:
            fb = Feedback(
                doctor_id  = doctor.id,
                patient_id = patient.id,
                bill_id    = bill.id,
                token      = secrets.token_urlsafe(16),
            )
            db.add(fb)
            db.commit()
            db.refresh(fb)
        base = settings.PUBLIC_BASE_URL.rstrip("/")
        return f"{base}/feedback/{fb.token}"
    except Exception as e:
        logger.warning(f"Feedback link creation failed for bill #{bill.id}: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        return ""


# ------------------------------------------------------------------ #
#  Background-task wrappers                                            #
#                                                                      #
#  These run AFTER the HTTP response is sent (via FastAPI              #
#  BackgroundTasks) so the WhatsApp API round-trip never blocks the   #
#  user's click. Each opens its OWN session — the request-scoped       #
#  session is already closed by the time these run. Never raise.       #
# ------------------------------------------------------------------ #

def send_appointment_confirmed_bg(appt_id: int, doctor_id: int):
    from database.connection import SessionLocal
    from database.models import Doctor
    db = SessionLocal()
    try:
        appt   = db.get(Appointment, appt_id)
        doctor = db.get(Doctor, doctor_id)
        if appt and doctor:
            notify_appointment_confirmed(appt, doctor, db)
    except Exception as e:
        logger.warning(f"bg confirmation failed (appt #{appt_id}): {e}")
    finally:
        db.close()


def send_followup_confirmed_bg(appt_id: int, doctor_id: int):
    from database.connection import SessionLocal
    from database.models import Doctor
    db = SessionLocal()
    try:
        appt   = db.get(Appointment, appt_id)
        doctor = db.get(Doctor, doctor_id)
        if appt and doctor:
            notify_followup_confirmed(appt, doctor, db)
    except Exception as e:
        logger.warning(f"bg follow-up failed (appt #{appt_id}): {e}")
    finally:
        db.close()


def send_walkin_queued_bg(visit_id: int, doctor_id: int):
    from database.connection import SessionLocal
    from database.models import Doctor, Visit
    db = SessionLocal()
    try:
        visit  = db.get(Visit, visit_id)
        doctor = db.get(Doctor, doctor_id)
        if visit and doctor:
            notify_walkin_queued(visit, doctor, db)
    except Exception as e:
        logger.warning(f"bg walk-in queue failed (visit #{visit_id}): {e}")
    finally:
        db.close()


def send_bill_receipt_bg(bill_id: int, doctor_id: int):
    from database.connection import SessionLocal
    from database.models import Doctor, Bill
    db = SessionLocal()
    try:
        bill   = db.get(Bill, bill_id)
        doctor = db.get(Doctor, doctor_id)
        if bill and doctor:
            notify_bill_receipt(bill, doctor, db)
    except Exception as e:
        logger.warning(f"bg bill receipt failed (bill #{bill_id}): {e}")
    finally:
        db.close()
