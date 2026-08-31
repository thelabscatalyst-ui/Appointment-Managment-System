"""
helpers.py — shared setup for the endpoint suites.

One place to build the state every suite needs (a verified, paying doctor with
a schedule; a patient; an appointment; a visit in the queue) so the tests
themselves only contain the thing under test.

Nothing here sends email: conftest blanks RESEND_API_KEY for the whole suite,
and no helper touches the notification services.
"""
from datetime import datetime, timedelta, date, time

from tests.conftest import TestSessionLocal
from database.models import (Doctor, Clinic, ClinicDoctor, Patient, Appointment,
                             AppointmentStatus, DoctorSchedule, Visit, VisitStatus)

PASSWORD = "Kv9$mPq2#Zx8L"

_seq = [0]


def uniq(prefix="x"):
    _seq[0] += 1
    return f"{prefix}{_seq[0]}"


def phone():
    _seq[0] += 1
    return str(9600000000 + _seq[0])


# --------------------------------------------------------------------------- #
#  Accounts                                                                     #
# --------------------------------------------------------------------------- #

def register(client, email, *, name="Dr Test", clinic_name="Test Clinic",
             account_type="solo", password=PASSWORD, city="Mumbai",
             clinic_invite="", phone_no=None):
    return client.post("/register", data={
        "name": name, "email": email, "phone": phone_no or phone(),
        "password": password, "clinic_name": clinic_name, "city": city,
        "specialization": "General", "clinic_invite": clinic_invite,
        "account_type": account_type,
    }, follow_redirects=False)


def verify_email(email):
    db = TestSessionLocal()
    try:
        d = db.query(Doctor).filter(Doctor.email == email.strip().lower()).first()
        if d and not d.email_verified_at:
            d.email_verified_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()


def login(client, email, password=PASSWORD):
    return client.post("/login", data={"email": email, "password": password},
                       follow_redirects=False)


def make_doctor(client, email, **kw):
    """Register + verify + log in. Returns the doctor id.

    Leaves `client` authenticated as this doctor.
    """
    register(client, email, **kw)
    verify_email(email)
    r = login(client, email)
    assert r.status_code == 303, f"login failed: {r.status_code} {r.text[:200]}"
    db = TestSessionLocal()
    try:
        return db.query(Doctor).filter(Doctor.email == email).first().id
    finally:
        db.close()


def clinic_of(doctor_id):
    db = TestSessionLocal()
    try:
        c = db.query(Clinic).filter(Clinic.owner_doctor_id == doctor_id).first()
        return c.id if c else None
    finally:
        db.close()


def set_pin(client, pin="123456"):
    """Set and unlock a PIN — several routes sit behind require_pin."""
    client.post("/doctors/settings/pin", data={"new_pin": pin, "confirm_pin": pin},
                follow_redirects=False)
    return client.post("/pin-prompt", data={"pin": pin, "next": "/dashboard"},
                       follow_redirects=False)


# --------------------------------------------------------------------------- #
#  Clinical data                                                                #
# --------------------------------------------------------------------------- #

def give_schedule(doctor_id, clinic_id=None, *, days=range(7),
                  start=time(9, 0), end=time(21, 0)):
    """Open hours wide enough that slot checks never fight the test."""
    db = TestSessionLocal()
    try:
        for d in days:
            db.add(DoctorSchedule(
                doctor_id=doctor_id, clinic_id=clinic_id, day_of_week=d,
                start_time=start, end_time=end, slot_duration=15,
                max_patients=100, walk_in_buffer=0, is_active=True))
        db.commit()
    finally:
        db.close()


def make_patient(doctor_id, clinic_id=None, *, name="Test Patient", ph=None):
    db = TestSessionLocal()
    try:
        p = Patient(doctor_id=doctor_id, clinic_id=clinic_id, name=name,
                    phone=ph or phone(), age=40, gender="male")
        db.add(p); db.commit(); db.refresh(p)
        return p.id
    finally:
        db.close()


def make_appointment(doctor_id, patient_id, clinic_id=None, *,
                     on=None, at=time(11, 0), status=AppointmentStatus.scheduled):
    db = TestSessionLocal()
    try:
        a = Appointment(doctor_id=doctor_id, patient_id=patient_id,
                        clinic_id=clinic_id, appointment_date=on or date.today(),
                        appointment_time=at, duration_mins=15, status=status)
        db.add(a); db.commit(); db.refresh(a)
        return a.id
    finally:
        db.close()


def make_visit(doctor_id, patient_id, clinic_id=None, *, appointment_id=None,
               status=VisitStatus.waiting, position=1, token=1):
    db = TestSessionLocal()
    try:
        v = Visit(doctor_id=doctor_id, patient_id=patient_id, clinic_id=clinic_id,
                  appointment_id=appointment_id, visit_date=date.today(),
                  status=status, queue_position=position, token_number=token,
                  check_in_time=datetime.utcnow())
        db.add(v); db.commit(); db.refresh(v)
        return v.id
    finally:
        db.close()


# --------------------------------------------------------------------------- #
#  Readback                                                                     #
# --------------------------------------------------------------------------- #

def visit_row(visit_id):
    db = TestSessionLocal()
    try:
        v = db.query(Visit).filter(Visit.id == visit_id).first()
        if v is None:
            return None
        return {"status": v.status, "position": v.queue_position,
                "emergency": v.is_emergency, "doctor_id": v.doctor_id,
                "call_time": v.call_time, "clinic_id": v.clinic_id}
    finally:
        db.close()


def appt_row(appt_id):
    db = TestSessionLocal()
    try:
        a = db.query(Appointment).filter(Appointment.id == appt_id).first()
        return None if a is None else {"status": a.status, "doctor_id": a.doctor_id,
                                       "clinic_id": a.clinic_id,
                                       "date": a.appointment_date}
    finally:
        db.close()


def count(model, **filters):
    db = TestSessionLocal()
    try:
        q = db.query(model)
        for k, v in filters.items():
            q = q.filter(getattr(model, k) == v)
        return q.count()
    finally:
        db.close()
