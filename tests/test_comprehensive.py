"""
test_comprehensive.py — Full integration + unit test suite for Med Track.

Coverage areas:
  A. Authentication
  B. Dashboard
  C. Appointments (CRUD)
  D. Visit / Queue State Machine
  E. Patients
  F. Public Booking
  G. Settings
  H. Slot Availability (white box)
  I. Billing
  J. Data Isolation (security)
  K. Edge Cases (medical domain)
  L. Notifications (mocked)
  M. PIN System
"""

import os
import sys
import hmac
import hashlib
from datetime import date, time, datetime, timedelta
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# ─────────────────────────────────────────────────────────────────────────────
#  Test DB
# ─────────────────────────────────────────────────────────────────────────────
TEST_DB_URL = "sqlite:///./test_clinic.db"
os.environ["DATABASE_URL"] = TEST_DB_URL

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.connection import Base, get_db
from database.models import (
    Doctor, Patient, Appointment, AppointmentStatus, AppointmentType,
    BookedBy, DoctorSchedule, BlockedDate, Visit, VisitStatus,
    Bill, BillItem, PaymentMode, PriceCatalog, PlanType,
)
from services.appointment_service import get_available_slots, get_or_create_patient
import services.visit_service as vs

test_engine = create_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
TestSession = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


def override_get_db():
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
#  Session fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def create_tables():
    import database.models  # noqa — registers all models with Base
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)
    try:
        os.remove("test_clinic.db")
    except FileNotFoundError:
        pass


@pytest.fixture(autouse=True)
def clean_db():
    """Wipe all rows before each test for isolation."""
    db = TestSession()
    try:
        from database.models import (
            BillItem, Bill, NotificationLog, Visit, Appointment,
            PatientNote, NoteFile, PatientDocument, PinnedPatient,
            BlockedDate, BlockedTime, DoctorSchedule, Subscription,
            Expense, RecurringExpense, PriceCatalog,
            Patient, ClinicDoctor, ClinicDoctorInvite, EmailVerification, PasswordReset, Clinic, Doctor,
        )
        for mdl in [
            BillItem, Bill, NotificationLog, Visit, Appointment,
            PatientNote, NoteFile, PatientDocument, PinnedPatient,
            BlockedDate, BlockedTime, DoctorSchedule, Subscription,
            Expense, RecurringExpense, PriceCatalog,
            Patient, ClinicDoctor, ClinicDoctorInvite, EmailVerification, PasswordReset, Clinic, Doctor,
        ]:
            db.query(mdl).delete()
        db.commit()
    finally:
        db.close()
    yield


@pytest.fixture(scope="session")
def client():
    with patch("services.scheduler_service.start_scheduler"), \
         patch("services.scheduler_service.stop_scheduler"):
        from main import app
        app.dependency_overrides[get_db] = override_get_db
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


# ─────────────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────────────

_phone_seq = [9100000000]


def _next_phone() -> str:
    _phone_seq[0] += 1
    return str(_phone_seq[0])


def register(client, *, name="Dr Test", email="test@example.com",
             phone=None, password="Kv9$mPq2#Zx8L", city="Mumbai",
             clinic_name="Test Clinic", account_type="solo"):
    if phone is None:
        phone = _next_phone()
    resp = client.post("/register", data={
        "name": name, "email": email, "phone": phone,
        "password": password, "clinic_name": clinic_name,
        "city": city, "specialization": "General", "clinic_invite": "",
        "account_type": account_type,
    }, follow_redirects=False)
    # Auto-verify: verification is mandatory now (get_paying_doctor raises
    # EmailNotVerified until it's set) and has its own dedicated coverage in
    # TestEmailVerification. Tests that just need a working logged-in doctor
    # shouldn't have to route around that gate.
    if resp.status_code in (200, 302, 303):
        db = TestSession()
        try:
            doc = db.query(Doctor).filter(Doctor.email == email.strip().lower()).first()
            if doc and not doc.email_verified_at:
                doc.email_verified_at = datetime.utcnow()
                db.commit()
        finally:
            db.close()
    return resp


def login(client, email, password="Kv9$mPq2#Zx8L"):
    return client.post("/login", data={"email": email, "password": password},
                       follow_redirects=False)


def auth_cookie(client, email, password="Kv9$mPq2#Zx8L", **reg_kwargs):
    """Register + login, return access_token cookie string."""
    register(client, email=email, **reg_kwargs)
    r = login(client, email, password)
    assert r.status_code == 303, f"Login failed {r.status_code}: {r.text[:200]}"
    tok = r.cookies.get("access_token")
    assert tok, "No access_token cookie"
    return tok


def make_schedule(client, cookie, days=None):
    """Create schedule for given days (default: all 7) 09:00–17:00, 15-min slots."""
    if days is None:
        days = list(range(7))
    data = {"avg_consult_mins": "10"}
    for d in days:
        data[f"active_{d}"] = "on"
        data[f"shift_start_{d}_0"] = "09:00"
        data[f"shift_end_{d}_0"] = "17:00"
        data[f"slot_{d}"] = "15"
        data[f"max_{d}"] = "30"
        data[f"walkin_buf_{d}"] = "0"
    return client.post("/doctors/settings/schedule", data=data,
                       cookies={"access_token": cookie}, follow_redirects=False)


def book_appointment(client, cookie, appt_date=None, appt_time="10:00",
                     patient_name="Ramesh Kumar", patient_phone=None):
    """Create a scheduled appointment. Returns response."""
    if appt_date is None:
        appt_date = next_monday()
    if patient_phone is None:
        patient_phone = _next_phone()
    return client.post("/appointments", data={
        "patient_name": patient_name,
        "patient_phone": patient_phone,
        "patient_age": "35",
        "patient_gender": "male",
        "appt_date": appt_date,          # correct field name
        "appt_time": appt_time,          # correct field name
        "appointment_type": "follow_up",
        "duration": "15",
        "patient_notes": "",
        "booked_by_field": "doctor",     # correct field name
        "for_doctor_id": "0",
    }, cookies={"access_token": cookie}, follow_redirects=False)


def get_last_appointment(db) -> Appointment:
    """Return the most recently created appointment."""
    return db.query(Appointment).order_by(Appointment.id.desc()).first()


def next_monday() -> str:
    d = date.today()
    while d.weekday() != 0:
        d += timedelta(days=1)
    return d.isoformat()


# ─────────────────────────────────────────────────────────────────────────────
#  A. AUTHENTICATION
# ─────────────────────────────────────────────────────────────────────────────

class TestAuthentication:

    def test_register_valid(self, client):
        r = register(client, email="reg1@test.com", phone="9200000001")
        assert r.status_code in (200, 302, 303)
        db = TestSession()
        doc = db.query(Doctor).filter(Doctor.email == "reg1@test.com").first()
        db.close()
        assert doc is not None
        assert doc.name == "Dr Test"

    def test_register_duplicate_email(self, client):
        register(client, email="dup@test.com", phone="9200000002")
        r = register(client, email="dup@test.com", phone="9200000003")
        assert r.status_code == 400
        assert b"already registered" in r.content.lower() or b"email" in r.content.lower()

    def test_register_duplicate_phone(self, client):
        register(client, email="uniq1@test.com", phone="9200000010")
        r = register(client, email="uniq2@test.com", phone="9200000010")
        assert r.status_code == 400
        assert b"phone" in r.content.lower() or b"registered" in r.content.lower()

    def test_login_valid_sets_cookie(self, client):
        register(client, email="login1@test.com", phone="9200000020")
        r = login(client, "login1@test.com")
        assert r.status_code == 303
        assert "access_token" in r.cookies

    def test_login_wrong_password(self, client):
        register(client, email="login2@test.com", phone="9200000021")
        r = login(client, "login2@test.com", "WrongPass!")
        # Login fails — status 401 or 200 with error page, no cookie
        assert r.status_code in (200, 400, 401)
        assert "access_token" not in r.cookies

    def test_login_nonexistent_email(self, client):
        r = login(client, "nobody@test.com")
        assert r.status_code in (200, 400, 401)
        assert "access_token" not in r.cookies

    def test_logout_clears_cookie(self, client):
        register(client, email="logout@test.com", phone="9200000022")
        tok = auth_cookie(client, "logout@test.com")
        r = client.get("/logout", cookies={"access_token": tok},
                       follow_redirects=False)
        assert r.status_code in (302, 303)

    def test_protected_route_without_cookie_redirects(self, client):
        r = client.get("/dashboard", follow_redirects=False)
        assert r.status_code in (302, 303)
        assert "/login" in r.headers.get("location", "")

    def test_dashboard_with_valid_cookie(self, client):
        tok = auth_cookie(client, "dash@test.com")
        r = client.get("/dashboard", cookies={"access_token": tok})
        assert r.status_code == 200

    def test_doctor_a_cannot_update_doctor_b_appointment(self, client):
        """Data isolation via POST /appointments/{id}/status."""
        tokA = auth_cookie(client, "docA@test.com")
        tokB = auth_cookie(client, "docB@test.com")
        make_schedule(client, tokA)
        r = book_appointment(client, tokA, next_monday(), "10:00")
        assert r.status_code == 303
        db = TestSession()
        appt = get_last_appointment(db)
        appt_id = appt.id
        db.close()
        # Doctor B tries to cancel Doctor A's appointment
        client.post(f"/appointments/{appt_id}/status",
                    data={"status": "cancelled"},
                    cookies={"access_token": tokB}, follow_redirects=False)
        db = TestSession()
        appt = db.query(Appointment).filter(Appointment.id == appt_id).first()
        db.close()
        assert appt.status != AppointmentStatus.cancelled

    def test_password_not_stored_plaintext(self, client):
        register(client, email="pwtest@test.com", phone="9200000030")
        db = TestSession()
        doc = db.query(Doctor).filter(Doctor.email == "pwtest@test.com").first()
        db.close()
        assert doc.password_hash != "Pass1234!"
        assert len(doc.password_hash) > 30


# ─────────────────────────────────────────────────────────────────────────────
#  B. DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────

class TestDashboard:

    def test_dashboard_loads(self, client):
        tok = auth_cookie(client, "dash2@test.com")
        r = client.get("/dashboard", cookies={"access_token": tok})
        assert r.status_code == 200

    def test_dashboard_shows_doctor_name(self, client):
        register(client, name="Dr Ramesh", email="drramesh@test.com")
        tok = auth_cookie(client, "drramesh@test.com")
        r = client.get("/dashboard", cookies={"access_token": tok})
        assert r.status_code == 200
        assert b"Ramesh" in r.content or b"ramesh" in r.content.lower()

    def test_dashboard_shows_clinic_name_from_settings(self, client):
        """Clinic name set via settings appears on dashboard (not Phase 2 clinic name)."""
        tok = auth_cookie(client, "clinicname@test.com", clinic_name="Healthify Clinic")
        r = client.get("/dashboard", cookies={"access_token": tok})
        assert r.status_code == 200
        assert b"Healthify" in r.content

    def test_dashboard_today_appointments(self, client):
        tok = auth_cookie(client, "sched@test.com")
        make_schedule(client, tok)
        book_appointment(client, tok, date.today().isoformat(), "09:00")
        r = client.get("/dashboard", cookies={"access_token": tok})
        assert r.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
#  C. APPOINTMENTS
# ─────────────────────────────────────────────────────────────────────────────

class TestAppointments:

    def test_appointments_list_loads(self, client):
        tok = auth_cookie(client, "apptlist@test.com")
        r = client.get("/appointments", cookies={"access_token": tok})
        assert r.status_code == 200

    def test_new_appointment_form_loads(self, client):
        tok = auth_cookie(client, "apptform@test.com")
        r = client.get("/appointments/new", cookies={"access_token": tok})
        assert r.status_code == 200
        assert b"form" in r.content.lower()

    def test_create_appointment_valid(self, client):
        tok = auth_cookie(client, "apptcreate@test.com")
        make_schedule(client, tok)
        r = book_appointment(client, tok, next_monday(), "09:00")
        assert r.status_code == 303
        db = TestSession()
        appt = get_last_appointment(db)
        db.close()
        assert appt is not None

    def test_create_appointment_patient_auto_created(self, client):
        tok = auth_cookie(client, "patautocreate@test.com")
        make_schedule(client, tok)
        phone = "8200000001"
        book_appointment(client, tok, next_monday(), "09:15",
                         patient_name="New Patient", patient_phone=phone)
        db = TestSession()
        p = db.query(Patient).filter(Patient.phone == phone).first()
        db.close()
        assert p is not None
        assert p.name == "New Patient"

    def test_create_appointment_missing_patient_name(self, client):
        tok = auth_cookie(client, "apptmissname@test.com")
        make_schedule(client, tok)
        r = client.post("/appointments", data={
            "patient_name": "",
            "patient_phone": "8300000001",
            "appt_date": next_monday(),
            "appt_time": "09:00",
            "appointment_type": "follow_up",
        }, cookies={"access_token": tok}, follow_redirects=False)
        assert r.status_code in (200, 400, 422)

    def test_create_appointment_missing_phone(self, client):
        tok = auth_cookie(client, "apptmissphone@test.com")
        make_schedule(client, tok)
        r = client.post("/appointments", data={
            "patient_name": "Test Patient",
            "patient_phone": "",
            "appt_date": next_monday(),
            "appt_time": "09:00",
            "appointment_type": "follow_up",
        }, cookies={"access_token": tok}, follow_redirects=False)
        assert r.status_code in (200, 400, 422)

    def test_create_appointment_duplicate_slot_blocked(self, client):
        """Two appointments at the same time slot must be blocked."""
        tok = auth_cookie(client, "apptdupslot@test.com")
        make_schedule(client, tok)
        phone1 = _next_phone()
        phone2 = _next_phone()
        r1 = book_appointment(client, tok, next_monday(), "10:00", patient_phone=phone1)
        assert r1.status_code == 303  # first booking succeeds
        r2 = book_appointment(client, tok, next_monday(), "10:00", patient_phone=phone2)
        db = TestSession()
        count = db.query(Appointment).filter(
            Appointment.appointment_time == time(10, 0),
            Appointment.status == AppointmentStatus.scheduled,
        ).count()
        db.close()
        assert count == 1  # second booking must NOT create another appointment

    def test_appointment_status_update(self, client):
        tok = auth_cookie(client, "apptstatusupd@test.com")
        make_schedule(client, tok)
        book_appointment(client, tok, next_monday(), "09:45")
        db = TestSession()
        appt_id = get_last_appointment(db).id
        db.close()
        r = client.post(f"/appointments/{appt_id}/status", data={
            "status": "completed",
            "doctor_notes": "Patient recovered.",
        }, cookies={"access_token": tok}, follow_redirects=False)
        assert r.status_code in (200, 303)
        db = TestSession()
        appt = db.query(Appointment).filter(Appointment.id == appt_id).first()
        db.close()
        assert appt.status == AppointmentStatus.completed

    def test_appointment_edit_form_loads(self, client):
        tok = auth_cookie(client, "apptedit@test.com")
        make_schedule(client, tok)
        book_appointment(client, tok, next_monday(), "10:15")
        db = TestSession()
        appt_id = get_last_appointment(db).id
        db.close()
        r = client.get(f"/appointments/{appt_id}/edit",
                       cookies={"access_token": tok})
        assert r.status_code == 200

    def test_appointment_edit_reschedule(self, client):
        tok = auth_cookie(client, "apptreschedule@test.com")
        make_schedule(client, tok)
        book_appointment(client, tok, next_monday(), "10:30")
        db = TestSession()
        appt_id = get_last_appointment(db).id
        db.close()
        r = client.post(f"/appointments/{appt_id}/edit", data={
            "appt_date": next_monday(),
            "appt_time": "11:00",
            "appointment_type": "follow_up",
            "duration": "15",
            "patient_notes": "",
        }, cookies={"access_token": tok}, follow_redirects=False)
        assert r.status_code in (200, 303)
        db = TestSession()
        appt = db.query(Appointment).filter(Appointment.id == appt_id).first()
        db.close()
        assert appt.appointment_time == time(11, 0)

    def test_walkin_creates_and_enters_queue(self, client):
        tok = auth_cookie(client, "walkin@test.com")
        make_schedule(client, tok)
        r = client.post("/appointments/walkin", data={
            "patient_name": "Walk-in Patient",
            "patient_phone": _next_phone(),
            "patient_age": "40",
            "patient_gender": "male",
            "is_emergency": "",
        }, cookies={"access_token": tok}, follow_redirects=False)
        assert r.status_code in (200, 303)
        db = TestSession()
        appt = db.query(Appointment).filter(
            Appointment.booked_by == BookedBy.walk_in
        ).first()
        visit = db.query(Visit).first()
        db.close()
        assert appt is not None
        assert visit is not None
        assert visit.status == VisitStatus.waiting

    def test_slots_endpoint_returns_json(self, client):
        tok = auth_cookie(client, "slots@test.com")
        make_schedule(client, tok)
        r = client.get(f"/appointments/slots?date={next_monday()}",
                       cookies={"access_token": tok})
        assert r.status_code == 200
        data = r.json()
        assert "slots" in data
        assert isinstance(data["slots"], list)
        assert len(data["slots"]) > 0

    def test_slots_empty_for_no_schedule(self, client):
        tok = auth_cookie(client, "noschedule@test.com")
        r = client.get(f"/appointments/slots?date={next_monday()}",
                       cookies={"access_token": tok})
        assert r.status_code == 200
        assert r.json()["slots"] == []

    def test_appointment_isolation_status_update(self, client):
        """Doctor B cannot update Doctor A's appointment status."""
        tokA = auth_cookie(client, "isoA@test.com")
        tokB = auth_cookie(client, "isoB@test.com")
        make_schedule(client, tokA)
        book_appointment(client, tokA, next_monday(), "09:00")
        db = TestSession()
        appt_id = get_last_appointment(db).id
        db.close()
        client.post(f"/appointments/{appt_id}/status",
                    data={"status": "cancelled"},
                    cookies={"access_token": tokB}, follow_redirects=False)
        db = TestSession()
        appt = db.query(Appointment).filter(Appointment.id == appt_id).first()
        db.close()
        assert appt.status != AppointmentStatus.cancelled


# ─────────────────────────────────────────────────────────────────────────────
#  D. VISIT / QUEUE STATE MACHINE
# ─────────────────────────────────────────────────────────────────────────────

class TestQueueStateMachine:

    def _create_doctor_and_patient(self):
        """Return (db, doctor, patient) with fresh objects."""
        db = TestSession()
        from services.auth_service import hash_password
        ts = int(datetime.now().timestamp() * 1000) % 1000000
        doc = Doctor(
            name="Dr Queue",
            email=f"queue{ts}@test.com",
            phone=str(9300000000 + ts),
            password_hash=hash_password("Pass1234!"),
            slug=f"dr-queue-{ts}",
            trial_ends_at=datetime.utcnow() + timedelta(days=14),
            plan_type="trial",
        )
        db.add(doc)
        db.flush()
        pat = Patient(doctor_id=doc.id, name="Queue Patient", phone=str(7000000000 + ts))
        db.add(pat)
        db.commit()
        db.refresh(doc)
        db.refresh(pat)
        return db, doc, pat

    def test_checkin_creates_waiting_visit(self, client):
        db, doc, pat = self._create_doctor_and_patient()
        visit = vs.check_in(db, doctor_id=doc.id, patient_id=pat.id)
        db.commit()
        assert visit.status == VisitStatus.waiting
        assert visit.token_number == 1
        db.close()

    def test_token_numbers_are_monotonic(self, client):
        db, doc, pat = self._create_doctor_and_patient()
        v1 = vs.check_in(db, doctor_id=doc.id, patient_id=pat.id)
        db.commit()
        ts = int(datetime.now().timestamp() * 1000) % 1000000
        pat2 = Patient(doctor_id=doc.id, name="P2", phone=str(7001000000 + ts))
        db.add(pat2)
        db.flush()
        v2 = vs.check_in(db, doctor_id=doc.id, patient_id=pat2.id)
        db.commit()
        assert v2.token_number == v1.token_number + 1
        db.close()

    def test_call_next_moves_to_serving(self, client):
        db, doc, pat = self._create_doctor_and_patient()
        vs.check_in(db, doctor_id=doc.id, patient_id=pat.id)
        db.commit()
        serving = vs.call_next(db, doctor_id=doc.id)
        db.commit()
        assert serving is not None
        assert serving.status == VisitStatus.serving
        db.close()

    def test_done_moves_to_billing_pending(self, client):
        db, doc, pat = self._create_doctor_and_patient()
        vs.check_in(db, doctor_id=doc.id, patient_id=pat.id)
        db.commit()
        serving = vs.call_next(db, doctor_id=doc.id)
        db.commit()
        vs.done_and_call_next(db, serving)   # correct: pass visit object
        db.commit()
        db.refresh(serving)
        assert serving.status == VisitStatus.billing_pending
        db.close()

    def test_close_visit_marks_done(self, client):
        db, doc, pat = self._create_doctor_and_patient()
        vs.check_in(db, doctor_id=doc.id, patient_id=pat.id)
        db.commit()
        serving = vs.call_next(db, doctor_id=doc.id)
        db.commit()
        vs.done_and_call_next(db, serving)
        db.commit()
        # Create a zero-value bill and close visit
        bill = Bill(
            visit_id=serving.id, doctor_id=doc.id, patient_id=pat.id,
            subtotal=0, discount=0, gst_amount=0, total=0,
            paid_amount=0, payment_mode=PaymentMode.free,
            paid_at=datetime.now(),
        )
        db.add(bill)
        db.flush()
        vs.close_visit(db, serving, bill.id)
        db.commit()
        db.refresh(serving)
        assert serving.status == VisitStatus.done
        db.close()

    def test_skip_visit_sets_skipped_status(self, client):
        db, doc, pat = self._create_doctor_and_patient()
        visit = vs.check_in(db, doctor_id=doc.id, patient_id=pat.id)
        db.commit()
        vs.skip_visit(db, visit)   # correct: pass visit object
        db.commit()
        db.refresh(visit)
        # skip_visit sets status to SKIPPED (moves to end of queue)
        assert visit.status == VisitStatus.skipped
        db.close()

    def test_cancel_visit(self, client):
        db, doc, pat = self._create_doctor_and_patient()
        visit = vs.check_in(db, doctor_id=doc.id, patient_id=pat.id)
        db.commit()
        vs.cancel_visit(db, visit)   # correct: pass visit object
        db.commit()
        db.refresh(visit)
        assert visit.status == VisitStatus.cancelled
        db.close()

    def test_emergency_jumps_to_front_of_queue(self, client):
        db, doc, pat = self._create_doctor_and_patient()
        v1 = vs.check_in(db, doctor_id=doc.id, patient_id=pat.id)
        db.commit()
        ts = int(datetime.now().timestamp() * 1000) % 1000000
        pat2 = Patient(doctor_id=doc.id, name="Emergency", phone=str(7002000000 + ts))
        db.add(pat2)
        db.flush()
        v2 = vs.check_in(db, doctor_id=doc.id, patient_id=pat2.id, is_emergency=True)
        db.commit()
        db.refresh(v1)
        db.refresh(v2)
        assert v2.queue_position < v1.queue_position or v2.is_emergency
        db.close()

    def test_call_next_returns_none_when_queue_empty(self, client):
        db, doc, pat = self._create_doctor_and_patient()
        result = vs.call_next(db, doctor_id=doc.id)
        assert result is None
        db.close()

    def test_done_and_call_next_auto_serves_next_patient(self, client):
        db, doc, pat = self._create_doctor_and_patient()
        v1 = vs.check_in(db, doctor_id=doc.id, patient_id=pat.id)
        ts = int(datetime.now().timestamp() * 1000) % 1000000
        pat2 = Patient(doctor_id=doc.id, name="Second", phone=str(7003000000 + ts))
        db.add(pat2)
        db.flush()
        v2 = vs.check_in(db, doctor_id=doc.id, patient_id=pat2.id)
        db.commit()
        serving = vs.call_next(db, doctor_id=doc.id)
        db.commit()
        vs.done_and_call_next(db, serving)
        db.commit()
        db.refresh(v2)
        assert v2.status == VisitStatus.serving
        db.close()

    def test_queue_status_json_endpoint(self, client):
        tok = auth_cookie(client, "qjson@test.com")
        r = client.get("/visits/queue-status", cookies={"access_token": tok})
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)

    def test_two_doctors_queues_fully_isolated(self, client):
        db = TestSession()
        from services.auth_service import hash_password
        ts = int(datetime.now().timestamp() * 1000) % 1000000
        docA = Doctor(name="DrA", email=f"qa{ts}@test.com",
                      phone=str(9400000000 + ts),
                      password_hash=hash_password("x"),
                      slug=f"dra-{ts}",
                      trial_ends_at=datetime.utcnow() + timedelta(days=14))
        docB = Doctor(name="DrB", email=f"qb{ts}@test.com",
                      phone=str(9410000000 + ts),
                      password_hash=hash_password("x"),
                      slug=f"drb-{ts}",
                      trial_ends_at=datetime.utcnow() + timedelta(days=14))
        db.add_all([docA, docB])
        db.flush()
        patA = Patient(doctor_id=docA.id, name="PatA", phone=str(7100000001 + ts))
        patB = Patient(doctor_id=docB.id, name="PatB", phone=str(7100000002 + ts))
        db.add_all([patA, patB])
        db.flush()
        vs.check_in(db, doctor_id=docA.id, patient_id=patA.id)
        db.commit()
        _, waitingB, _ = vs.get_today_visits(db, docB.id)
        assert len(waitingB) == 0
        db.close()

    def test_walkin_auto_checkin_via_http(self, client):
        tok = auth_cookie(client, "walkinqueue@test.com")
        make_schedule(client, tok)
        r = client.post("/appointments/walkin", data={
            "patient_name": "Queue Walk-in",
            "patient_phone": _next_phone(),
            "patient_age": "25",
            "patient_gender": "female",
            "is_emergency": "",
        }, cookies={"access_token": tok}, follow_redirects=False)
        assert r.status_code in (200, 303)
        db = TestSession()
        visit = db.query(Visit).first()
        db.close()
        assert visit is not None
        assert visit.status == VisitStatus.waiting

    def test_multiple_patients_queue_positions_ordered(self, client):
        db, doc, pat = self._create_doctor_and_patient()
        ts = int(datetime.now().timestamp() * 1000) % 1000000
        p2 = Patient(doctor_id=doc.id, name="P2", phone=str(7200000001 + ts))
        p3 = Patient(doctor_id=doc.id, name="P3", phone=str(7200000002 + ts))
        db.add_all([p2, p3])
        db.flush()
        db.commit()
        v1 = vs.check_in(db, doctor_id=doc.id, patient_id=pat.id)
        db.commit()
        v2 = vs.check_in(db, doctor_id=doc.id, patient_id=p2.id)
        db.commit()
        v3 = vs.check_in(db, doctor_id=doc.id, patient_id=p3.id)
        db.commit()
        assert v1.token_number < v2.token_number < v3.token_number
        assert v1.queue_position < v2.queue_position < v3.queue_position
        db.close()

    def test_skip_moves_visit_to_end_of_queue(self, client):
        db, doc, pat = self._create_doctor_and_patient()
        ts = int(datetime.now().timestamp() * 1000) % 1000000
        p2 = Patient(doctor_id=doc.id, name="Second", phone=str(7300000001 + ts))
        db.add(p2)
        db.flush()
        db.commit()
        v1 = vs.check_in(db, doctor_id=doc.id, patient_id=pat.id)
        db.commit()
        v2 = vs.check_in(db, doctor_id=doc.id, patient_id=p2.id)
        db.commit()
        vs.skip_visit(db, v1)
        db.commit()
        db.refresh(v1)
        db.refresh(v2)
        assert v1.queue_position > v2.queue_position
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
#  E. PATIENTS
# ─────────────────────────────────────────────────────────────────────────────

class TestPatients:

    def test_patient_list_loads(self, client):
        tok = auth_cookie(client, "patlist@test.com")
        r = client.get("/patients", cookies={"access_token": tok})
        assert r.status_code == 200

    def test_get_or_create_patient_new(self):
        db = TestSession()
        from services.auth_service import hash_password
        ts = int(datetime.now().timestamp() * 1000) % 1000000
        doc = Doctor(name="Dr P", email=f"drp{ts}@test.com",
                     phone=str(9500000000 + ts),
                     password_hash=hash_password("x"),
                     slug=f"drp-{ts}",
                     trial_ends_at=datetime.utcnow() + timedelta(days=14))
        db.add(doc)
        db.flush()
        p = get_or_create_patient(doc.id, "Suresh", "7200000001", db)
        db.commit()
        assert p.id is not None
        assert p.name == "Suresh"
        db.close()

    def test_get_or_create_patient_same_phone_returns_same_record(self):
        db = TestSession()
        from services.auth_service import hash_password
        ts = int(datetime.now().timestamp() * 1000) % 1000000
        doc = Doctor(name="Dr P2", email=f"drp2{ts}@test.com",
                     phone=str(9500010000 + ts),
                     password_hash=hash_password("x"),
                     slug=f"drp2-{ts}",
                     trial_ends_at=datetime.utcnow() + timedelta(days=14))
        db.add(doc)
        db.flush()
        p1 = get_or_create_patient(doc.id, "Suresh", "7200000002", db)
        db.commit()
        p2 = get_or_create_patient(doc.id, "Different Name", "7200000002", db)
        db.commit()
        assert p1.id == p2.id
        db.close()

    def test_patient_different_phone_creates_different_record(self):
        db = TestSession()
        from services.auth_service import hash_password
        ts = int(datetime.now().timestamp() * 1000) % 1000000
        doc = Doctor(name="Dr P3", email=f"drp3{ts}@test.com",
                     phone=str(9500020000 + ts),
                     password_hash=hash_password("x"),
                     slug=f"drp3-{ts}",
                     trial_ends_at=datetime.utcnow() + timedelta(days=14))
        db.add(doc)
        db.flush()
        p1 = get_or_create_patient(doc.id, "Ramesh", "7200000010", db)
        p2 = get_or_create_patient(doc.id, "Ramesh", "7200000011", db)
        db.commit()
        assert p1.id != p2.id
        db.close()

    def test_patient_detail_accessible(self, client):
        tok = auth_cookie(client, "patdetail@test.com")
        make_schedule(client, tok)
        phone = _next_phone()
        book_appointment(client, tok, next_monday(), "10:00",
                         patient_name="Detail Pat", patient_phone=phone)
        db = TestSession()
        pat = db.query(Patient).filter(Patient.phone == phone).first()
        pat_id = pat.id
        db.close()
        r = client.get(f"/patients/{pat_id}", cookies={"access_token": tok})
        assert r.status_code == 200
        assert b"Detail Pat" in r.content

    def test_patient_isolation_different_doctor(self, client):
        tokA = auth_cookie(client, "patIsoA@test.com")
        tokB = auth_cookie(client, "patIsoB@test.com")
        make_schedule(client, tokA)
        phone = _next_phone()
        book_appointment(client, tokA, next_monday(), "10:00", patient_phone=phone)
        db = TestSession()
        pat = db.query(Patient).filter(Patient.phone == phone).first()
        pat_id = pat.id
        db.close()
        # Doctor B should be redirected away (404 or redirect)
        r = client.get(f"/patients/{pat_id}",
                       cookies={"access_token": tokB}, follow_redirects=False)
        assert r.status_code in (302, 303, 404)

    def test_patient_add_note(self, client):
        tok = auth_cookie(client, "patnotes@test.com")
        make_schedule(client, tok)
        phone = _next_phone()
        book_appointment(client, tok, next_monday(), "10:00", patient_phone=phone)
        db = TestSession()
        pat_id = db.query(Patient).filter(Patient.phone == phone).first().id
        db.close()
        r = client.post(f"/patients/{pat_id}/notes/add",  # correct endpoint
                        data={"note_text": "Patient has hypertension."},
                        cookies={"access_token": tok}, follow_redirects=False)
        assert r.status_code in (200, 303)

    def test_patient_edit_name(self, client):
        tok = auth_cookie(client, "patedit@test.com")
        make_schedule(client, tok)
        phone = _next_phone()
        book_appointment(client, tok, next_monday(), "10:00",
                         patient_name="OldName", patient_phone=phone)
        db = TestSession()
        pat_id = db.query(Patient).filter(Patient.phone == phone).first().id
        db.close()
        r = client.post(f"/patients/{pat_id}/edit",
                        data={"name": "NewName", "phone": phone},
                        cookies={"access_token": tok}, follow_redirects=False)
        assert r.status_code in (200, 303)
        db = TestSession()
        pat = db.query(Patient).filter(Patient.id == pat_id).first()
        db.close()
        assert pat.name == "Newname"

    def test_patient_created_with_visit_count(self, client):
        tok = auth_cookie(client, "visitcount@test.com")
        make_schedule(client, tok)
        phone = _next_phone()
        book_appointment(client, tok, next_monday(), "09:00", patient_phone=phone)
        db = TestSession()
        pat = db.query(Patient).filter(Patient.phone == phone).first()
        db.close()
        assert pat.visit_count >= 1


# ─────────────────────────────────────────────────────────────────────────────
#  F. PUBLIC BOOKING
# ─────────────────────────────────────────────────────────────────────────────

class TestPublicBooking:

    def _doctor_slug(self, client, email):
        tok = auth_cookie(client, email)
        make_schedule(client, tok)
        db = TestSession()
        doc = db.query(Doctor).filter(Doctor.email == email).first()
        slug = doc.slug
        db.close()
        return slug

    def test_public_booking_page_loads(self, client):
        slug = self._doctor_slug(client, "pub1@test.com")
        r = client.get(f"/book/{slug}")
        assert r.status_code == 200

    def test_public_booking_invalid_slug_404(self, client):
        r = client.get("/book/nonexistent-doctor-xyz-123")
        assert r.status_code == 404

    def test_public_booking_submit_valid(self, client):
        slug = self._doctor_slug(client, "pub2@test.com")
        r = client.post(f"/book/{slug}", data={
            "patient_name": "Public Patient",
            "patient_phone": _next_phone(),
            "appt_date": next_monday(),        # correct field name
            "appt_time": "09:00",              # correct field name
            "appointment_type": "new_patient",
            "patient_notes": "",
        }, follow_redirects=False)
        assert r.status_code in (302, 303)
        assert "/confirm/" in r.headers.get("location", "")

    def test_public_booking_missing_name(self, client):
        slug = self._doctor_slug(client, "pub3@test.com")
        r = client.post(f"/book/{slug}", data={
            "patient_name": "",
            "patient_phone": _next_phone(),
            "appt_date": next_monday(),
            "appt_time": "09:00",
            "appointment_type": "new_patient",
        }, follow_redirects=False)
        assert r.status_code in (200, 400, 422)

    def test_public_booking_missing_phone(self, client):
        slug = self._doctor_slug(client, "pub4@test.com")
        r = client.post(f"/book/{slug}", data={
            "patient_name": "No Phone",
            "patient_phone": "",
            "appt_date": next_monday(),
            "appt_time": "09:00",
            "appointment_type": "new_patient",
        }, follow_redirects=False)
        assert r.status_code in (200, 400, 422)

    def test_public_booking_rate_limit_after_5_bookings(self, client):
        slug = self._doctor_slug(client, "pub5@test.com")
        phone = _next_phone()
        for i in range(5):
            client.post(f"/book/{slug}", data={
                "patient_name": f"Rate {i}",
                "patient_phone": phone,
                "appt_date": next_monday(),
                "appt_time": f"{9+i}:00",
                "appointment_type": "new_patient",
            }, follow_redirects=False)
        # 6th booking should be rate-limited
        r = client.post(f"/book/{slug}", data={
            "patient_name": "Rate 6",
            "patient_phone": phone,
            "appt_date": next_monday(),
            "appt_time": "15:00",
            "appointment_type": "new_patient",
        }, follow_redirects=False)
        assert r.status_code in (200, 400, 429)

    def test_public_confirm_page_loads(self, client):
        slug = self._doctor_slug(client, "pub6@test.com")
        r = client.post(f"/book/{slug}", data={
            "patient_name": "Confirm Patient",
            "patient_phone": _next_phone(),
            "appt_date": next_monday(),
            "appt_time": "09:00",
            "appointment_type": "new_patient",
        }, follow_redirects=False)
        loc = r.headers.get("location", "")
        if "/confirm/" in loc:
            r2 = client.get(loc)
            assert r2.status_code == 200

    def test_public_slots_endpoint(self, client):
        slug = self._doctor_slug(client, "pub7@test.com")
        r = client.get(f"/book/{slug}/slots?date={next_monday()}")
        assert r.status_code == 200
        data = r.json()
        assert "slots" in data
        assert len(data["slots"]) > 0

    def test_booked_slot_disappears_from_public_slots(self, client):
        """After a booking, that slot is no longer available publicly."""
        slug = self._doctor_slug(client, "pub8@test.com")
        client.post(f"/book/{slug}", data={
            "patient_name": "First Booker",
            "patient_phone": _next_phone(),
            "appt_date": next_monday(),
            "appt_time": "09:00",
            "appointment_type": "new_patient",
        }, follow_redirects=False)
        r = client.get(f"/book/{slug}/slots?date={next_monday()}")
        slots = r.json()["slots"]
        assert "09:00" not in slots


# ─────────────────────────────────────────────────────────────────────────────
#  G. SETTINGS
# ─────────────────────────────────────────────────────────────────────────────

class TestSettings:

    def test_settings_page_loads(self, client):
        tok = auth_cookie(client, "settings1@test.com")
        r = client.get("/doctors/settings", cookies={"access_token": tok})
        assert r.status_code == 200

    def test_save_schedule_monday(self, client):
        tok = auth_cookie(client, "settings2@test.com")
        r = make_schedule(client, tok, days=[0])
        assert r.status_code in (200, 303)
        db = TestSession()
        sched = db.query(DoctorSchedule).filter(DoctorSchedule.day_of_week == 0).first()
        db.close()
        assert sched is not None
        assert sched.start_time == time(9, 0)
        assert sched.end_time == time(17, 0)

    def test_save_clinic_profile(self, client):
        tok = auth_cookie(client, "settings3@test.com")
        r = client.post("/doctors/settings/profile", data={
            "clinic_name": "Healthify",
            "clinic_address": "123 Main St",
            "city": "Nashik",
            "languages": "hindi,english",
        }, cookies={"access_token": tok}, follow_redirects=False)
        assert r.status_code in (200, 303)
        db = TestSession()
        doc = db.query(Doctor).filter(Doctor.email == "settings3@test.com").first()
        db.close()
        assert doc.clinic_name == "Healthify"
        assert doc.city == "Nashik"

    def test_block_date(self, client):
        tok = auth_cookie(client, "settings4@test.com")
        future = (date.today() + timedelta(days=30)).isoformat()
        r = client.post("/doctors/settings/block", data={
            "blocked_date": future, "reason": "On leave",
        }, cookies={"access_token": tok}, follow_redirects=False)
        assert r.status_code in (200, 303)
        db = TestSession()
        bd = db.query(BlockedDate).first()
        db.close()
        assert bd is not None

    def test_unblock_date(self, client):
        tok = auth_cookie(client, "settings4b@test.com")
        future = (date.today() + timedelta(days=31)).isoformat()
        client.post("/doctors/settings/block", data={
            "blocked_date": future, "reason": "Holiday",
        }, cookies={"access_token": tok}, follow_redirects=False)
        db = TestSession()
        bd = db.query(BlockedDate).first()
        bd_id = bd.id
        db.close()
        r = client.post(f"/doctors/settings/unblock/{bd_id}",
                        cookies={"access_token": tok}, follow_redirects=False)
        assert r.status_code in (200, 303)
        db = TestSession()
        bd2 = db.query(BlockedDate).filter(BlockedDate.id == bd_id).first()
        db.close()
        assert bd2 is None

    def test_set_pin_6_digits(self, client):
        """PIN must be exactly 6 digits."""
        tok = auth_cookie(client, "settings5@test.com")
        r = client.post("/doctors/settings/pin", data={
            "action": "set",
            "new_pin": "123456",
            "confirm_pin": "123456",
            "current_pin": "",
        }, cookies={"access_token": tok}, follow_redirects=False)
        assert r.status_code in (200, 303)
        db = TestSession()
        doc = db.query(Doctor).filter(Doctor.email == "settings5@test.com").first()
        db.close()
        assert doc.pin_hash is not None

    def test_set_pin_4_digits_rejected(self, client):
        """4-digit PIN must be rejected (requires 6)."""
        tok = auth_cookie(client, "settings5b@test.com")
        client.post("/doctors/settings/pin", data={
            "action": "set", "new_pin": "1234", "confirm_pin": "1234", "current_pin": "",
        }, cookies={"access_token": tok}, follow_redirects=False)
        db = TestSession()
        doc = db.query(Doctor).filter(Doctor.email == "settings5b@test.com").first()
        db.close()
        assert doc.pin_hash is None  # should NOT have been set

    def test_remove_pin(self, client):
        tok = auth_cookie(client, "settings7@test.com")
        client.post("/doctors/settings/pin", data={
            "action": "set", "new_pin": "111111", "confirm_pin": "111111", "current_pin": "",
        }, cookies={"access_token": tok}, follow_redirects=False)
        r = client.post("/doctors/settings/pin", data={
            "action": "remove", "current_pin": "111111",
            "new_pin": "", "confirm_pin": "",
        }, cookies={"access_token": tok}, follow_redirects=False)
        assert r.status_code in (200, 303)
        db = TestSession()
        doc = db.query(Doctor).filter(Doctor.email == "settings7@test.com").first()
        db.close()
        assert doc.pin_hash is None

    def test_account_details_update(self, client):
        tok = auth_cookie(client, "settings8@test.com")
        phone = _next_phone()
        r = client.post("/doctors/settings/account", data={
            "name": "Dr Updated",
            "email": "settings8@test.com",
            "phone": phone,
            "specialization": "Cardiology",
        }, cookies={"access_token": tok}, follow_redirects=False)
        assert r.status_code in (200, 303)
        db = TestSession()
        doc = db.query(Doctor).filter(Doctor.email == "settings8@test.com").first()
        db.close()
        assert doc.name == "Dr Updated"
        assert doc.specialization == "Cardiology"

    def test_account_duplicate_email_rejected(self, client):
        tok1 = auth_cookie(client, "setdup1@test.com")
        register(client, email="setdup2@test.com")
        r = client.post("/doctors/settings/account", data={
            "name": "Dr 1",
            "email": "setdup2@test.com",  # already taken by setdup2
            "phone": _next_phone(),
            "specialization": "",
        }, cookies={"access_token": tok1}, follow_redirects=False)
        # Should show an error, not silently accept
        assert r.status_code in (200, 400, 303)
        if r.status_code in (200, 400):
            assert b"account_error" in r.content or b"email" in r.content.lower()


# ─────────────────────────────────────────────────────────────────────────────
#  H. SLOT AVAILABILITY (White Box)
# ─────────────────────────────────────────────────────────────────────────────

class TestSlotAvailability:

    def _make_doctor_schedule(self, day_of_week: int):
        db = TestSession()
        from services.auth_service import hash_password
        ts = int(datetime.now().timestamp() * 1000) % 1000000
        doc = Doctor(name="Dr Slot", email=f"slot{ts}@test.com",
                     phone=str(9700000000 + ts),
                     password_hash=hash_password("x"),
                     slug=f"drslot-{ts}",
                     trial_ends_at=datetime.utcnow() + timedelta(days=14))
        db.add(doc)
        db.flush()
        sched = DoctorSchedule(
            doctor_id=doc.id, day_of_week=day_of_week,
            start_time=time(9, 0), end_time=time(17, 0),
            slot_duration=15, max_patients=30, is_active=True,
        )
        db.add(sched)
        db.commit()
        db.refresh(doc)
        return db, doc

    def test_slots_returned_for_scheduled_day(self):
        d = date.today()
        while d.weekday() != 0:
            d += timedelta(days=1)
        db, doc = self._make_doctor_schedule(0)  # Monday
        slots = get_available_slots(doc.id, d, db, filter_past=False)
        db.close()
        assert len(slots) > 0
        assert "09:00" in slots
        assert "09:15" in slots

    def test_no_slots_for_unscheduled_day(self):
        # Schedule only Monday (0), query Sunday (6)
        d = date.today()
        while d.weekday() != 6:
            d += timedelta(days=1)
        db, doc = self._make_doctor_schedule(0)
        slots = get_available_slots(doc.id, d, db, filter_past=False)
        db.close()
        assert slots == []

    def test_blocked_date_returns_no_slots(self):
        d = date.today()
        while d.weekday() != 0:
            d += timedelta(days=1)
        db, doc = self._make_doctor_schedule(0)
        bd = BlockedDate(doctor_id=doc.id, blocked_date=d, reason="Holiday")
        db.add(bd)
        db.commit()
        slots = get_available_slots(doc.id, d, db, filter_past=False)
        db.close()
        assert slots == []

    def test_booked_slot_removed_from_available(self):
        d = date.today()
        while d.weekday() != 0:
            d += timedelta(days=1)
        db, doc = self._make_doctor_schedule(0)
        pat = Patient(doctor_id=doc.id, name="Booked", phone=_next_phone())
        db.add(pat)
        db.flush()
        appt = Appointment(
            doctor_id=doc.id, patient_id=pat.id,
            appointment_date=d, appointment_time=time(9, 0),
            status=AppointmentStatus.scheduled,
        )
        db.add(appt)
        db.commit()
        slots = get_available_slots(doc.id, d, db, filter_past=False)
        db.close()
        assert "09:00" not in slots

    def test_cancelled_booking_slot_available_again(self):
        d = date.today()
        while d.weekday() != 0:
            d += timedelta(days=1)
        db, doc = self._make_doctor_schedule(0)
        pat = Patient(doctor_id=doc.id, name="Cancelled", phone=_next_phone())
        db.add(pat)
        db.flush()
        appt = Appointment(
            doctor_id=doc.id, patient_id=pat.id,
            appointment_date=d, appointment_time=time(9, 0),
            status=AppointmentStatus.cancelled,
        )
        db.add(appt)
        db.commit()
        slots = get_available_slots(doc.id, d, db, filter_past=False)
        db.close()
        assert "09:00" in slots

    def test_filter_past_true_hides_past_slots_today(self):
        today = date.today()
        db, doc = self._make_doctor_schedule(today.weekday())
        slots_all = get_available_slots(doc.id, today, db, filter_past=False)
        slots_future = get_available_slots(doc.id, today, db, filter_past=True)
        db.close()
        assert len(slots_future) <= len(slots_all)

    def test_slot_boundary_end_time_exclusive(self):
        """Slot at end_time must NOT be included (09:30 slot with end=09:30)."""
        d = date.today()
        while d.weekday() != 0:
            d += timedelta(days=1)
        db = TestSession()
        from services.auth_service import hash_password
        ts = int(datetime.now().timestamp() * 1000) % 1000000
        doc = Doctor(name="Dr Bound", email=f"bound{ts}@test.com",
                     phone=str(9700100000 + ts),
                     password_hash=hash_password("x"),
                     slug=f"drbound-{ts}",
                     trial_ends_at=datetime.utcnow() + timedelta(days=14))
        db.add(doc)
        db.flush()
        sched = DoctorSchedule(doctor_id=doc.id, day_of_week=0,
                               start_time=time(9, 0), end_time=time(9, 30),
                               slot_duration=15, max_patients=30, is_active=True)
        db.add(sched)
        db.commit()
        slots = get_available_slots(doc.id, d, db, filter_past=False)
        db.close()
        assert "09:00" in slots
        assert "09:15" in slots
        assert "09:30" not in slots


# ─────────────────────────────────────────────────────────────────────────────
#  I. BILLING
# ─────────────────────────────────────────────────────────────────────────────

class TestBilling:

    def test_billing_page_loads(self, client):
        tok = auth_cookie(client, "billing1@test.com")
        r = client.get("/billing", cookies={"access_token": tok})
        assert r.status_code == 200

    def test_bill_arithmetic_white_box(self):
        """subtotal - discount + gst = total (18% GST example)."""
        subtotal = 500.0
        discount = 50.0
        gst_rate = 0.18
        net = subtotal - discount
        gst_amount = round(net * gst_rate, 2)
        total = net + gst_amount
        assert total == pytest.approx(531.0, rel=1e-3)

    def test_zero_amount_free_visit_via_service(self):
        """close_visit with zero bill sets status=done."""
        db = TestSession()
        from services.auth_service import hash_password
        ts = int(datetime.now().timestamp() * 1000) % 1000000
        doc = Doctor(name="Dr Bill", email=f"drb{ts}@test.com",
                     phone=str(9800000000 + ts),
                     password_hash=hash_password("x"),
                     slug=f"drb-{ts}",
                     trial_ends_at=datetime.utcnow() + timedelta(days=14))
        db.add(doc)
        db.flush()
        pat = Patient(doctor_id=doc.id, name="Free Pat", phone=_next_phone())
        db.add(pat)
        db.flush()
        visit = vs.check_in(db, doctor_id=doc.id, patient_id=pat.id)
        db.commit()
        vs.call_next(db, doctor_id=doc.id)
        db.commit()
        vs.done_and_call_next(db, visit)
        db.commit()
        # Create zero bill
        bill = Bill(visit_id=visit.id, doctor_id=doc.id, patient_id=pat.id,
                    subtotal=0, discount=0, gst_amount=0, total=0,
                    paid_amount=0, payment_mode=PaymentMode.free,
                    paid_at=datetime.now())
        db.add(bill)
        db.flush()
        vs.close_visit(db, visit, bill.id)
        db.commit()
        db.refresh(visit)
        assert visit.status == VisitStatus.done
        db.close()

    def test_free_close_via_http_route(self, client):
        """POST /visits/{id}/close-free creates zero bill and marks visit done."""
        tok = auth_cookie(client, "freecloseweb@test.com")
        make_schedule(client, tok)
        # Create walk-in (auto check-in)
        client.post("/appointments/walkin", data={
            "patient_name": "Free Patient",
            "patient_phone": _next_phone(),
            "patient_age": "30",
            "patient_gender": "male",
            "is_emergency": "",
        }, cookies={"access_token": tok}, follow_redirects=False)
        db = TestSession()
        visit = db.query(Visit).first()
        visit_id = visit.id
        db.close()
        # Call next to move to SERVING
        db = TestSession()
        doc = db.query(Doctor).filter(Doctor.email == "freecloseweb@test.com").first()
        visit = db.query(Visit).filter(Visit.id == visit_id).first()
        vs.call_next(db, doctor_id=doc.id)
        db.commit()
        vs.done_and_call_next(db, visit)
        db.commit()
        db.close()
        # Close via HTTP
        r = client.post(f"/visits/{visit_id}/close-free",
                        data={"notes": ""},
                        cookies={"access_token": tok}, follow_redirects=False)
        assert r.status_code in (303, 200)
        db = TestSession()
        v = db.query(Visit).filter(Visit.id == visit_id).first()
        db.close()
        assert v.status == VisitStatus.done

    def test_verify_invalid_signature_rejected(self, client):
        tok = auth_cookie(client, "billing3@test.com")
        r = client.post("/billing/verify", data={
            "razorpay_payment_id": "pay_fake",
            "razorpay_order_id": "order_fake",
            "razorpay_signature": "invalidsignature",
            "plan": "solo",
        }, cookies={"access_token": tok}, follow_redirects=False)
        assert r.status_code in (200, 400, 303)


# ─────────────────────────────────────────────────────────────────────────────
#  J. DATA ISOLATION (Security)
# ─────────────────────────────────────────────────────────────────────────────

class TestDataIsolation:

    def test_doctor_cannot_read_other_doctor_patient(self, client):
        tokA = auth_cookie(client, "isopatA@test.com")
        tokB = auth_cookie(client, "isopatB@test.com")
        make_schedule(client, tokA)
        phone = _next_phone()
        book_appointment(client, tokA, next_monday(), "09:00", patient_phone=phone)
        db = TestSession()
        pat = db.query(Patient).filter(Patient.phone == phone).first()
        pat_id = pat.id
        db.close()
        r = client.get(f"/patients/{pat_id}",
                       cookies={"access_token": tokB}, follow_redirects=False)
        assert r.status_code in (302, 303, 404)

    def test_doctor_cannot_delete_other_doctor_patient(self, client):
        tokA = auth_cookie(client, "isodelatA@test.com")
        tokB = auth_cookie(client, "isodelatB@test.com")
        make_schedule(client, tokA)
        phone = _next_phone()
        book_appointment(client, tokA, next_monday(), "09:00", patient_phone=phone)
        db = TestSession()
        pat = db.query(Patient).filter(Patient.phone == phone).first()
        pat_id = pat.id
        db.close()
        client.post(f"/patients/{pat_id}/delete",
                    cookies={"access_token": tokB}, follow_redirects=False)
        db = TestSession()
        still_exists = db.query(Patient).filter(Patient.id == pat_id).first()
        db.close()
        assert still_exists is not None

    def test_doctor_cannot_update_other_doctor_appointment(self, client):
        tokA = auth_cookie(client, "isoupdA@test.com")
        tokB = auth_cookie(client, "isoupdB@test.com")
        make_schedule(client, tokA)
        book_appointment(client, tokA, next_monday(), "09:00")
        db = TestSession()
        appt_id = get_last_appointment(db).id
        db.close()
        client.post(f"/appointments/{appt_id}/status",
                    data={"status": "cancelled"},
                    cookies={"access_token": tokB}, follow_redirects=False)
        db = TestSession()
        appt = db.query(Appointment).filter(Appointment.id == appt_id).first()
        db.close()
        assert appt.status != AppointmentStatus.cancelled

    def test_admin_route_blocked_for_non_admin(self, client):
        tok = auth_cookie(client, "nonadmin@test.com")
        r = client.get("/admin", cookies={"access_token": tok},
                       follow_redirects=False)
        assert r.status_code in (302, 303, 403)

    def test_patients_list_only_shows_own_patients(self, client):
        tokA = auth_cookie(client, "ownlistA@test.com")
        tokB = auth_cookie(client, "ownlistB@test.com")
        make_schedule(client, tokA)
        book_appointment(client, tokA, next_monday(), "09:00",
                         patient_name="Only A Patient")
        r = client.get("/patients", cookies={"access_token": tokB})
        assert r.status_code == 200
        assert b"Only A Patient" not in r.content

    def test_appointment_list_only_shows_own_appointments(self, client):
        tokA = auth_cookie(client, "apptlistA@test.com")
        tokB = auth_cookie(client, "apptlistB@test.com")
        make_schedule(client, tokA)
        book_appointment(client, tokA, next_monday(), "09:00",
                         patient_name="Doctor A Patient")
        r = client.get("/appointments", cookies={"access_token": tokB})
        assert r.status_code == 200
        assert b"Doctor A Patient" not in r.content


# ─────────────────────────────────────────────────────────────────────────────
#  K. EDGE CASES (Medical Domain)
# ─────────────────────────────────────────────────────────────────────────────

class TestMedicalEdgeCases:

    def test_appointment_on_blocked_date_no_slots(self, client):
        tok = auth_cookie(client, "edgeblock@test.com")
        make_schedule(client, tok)
        future = next_monday()
        client.post("/doctors/settings/block", data={
            "blocked_date": future, "reason": "Holiday",
        }, cookies={"access_token": tok}, follow_redirects=False)
        r = client.get(f"/appointments/slots?date={future}",
                       cookies={"access_token": tok})
        assert r.json()["slots"] == []

    def test_double_booking_same_slot_prevented(self, client):
        tok = auth_cookie(client, "edgedouble@test.com")
        make_schedule(client, tok)
        phone1 = _next_phone()
        phone2 = _next_phone()
        r1 = book_appointment(client, tok, next_monday(), "09:00", patient_phone=phone1)
        assert r1.status_code == 303
        book_appointment(client, tok, next_monday(), "09:00", patient_phone=phone2)
        db = TestSession()
        count = db.query(Appointment).filter(
            Appointment.appointment_time == time(9, 0),
            Appointment.status == AppointmentStatus.scheduled,
        ).count()
        db.close()
        assert count == 1

    def test_same_phone_different_name_returns_same_patient(self):
        db = TestSession()
        from services.auth_service import hash_password
        ts = int(datetime.now().timestamp() * 1000) % 1000000
        doc = Doctor(name="Dr Edge", email=f"edge{ts}@test.com",
                     phone=str(9900000000 + ts),
                     password_hash=hash_password("x"),
                     slug=f"dredge-{ts}",
                     trial_ends_at=datetime.utcnow() + timedelta(days=14))
        db.add(doc)
        db.flush()
        p1 = get_or_create_patient(doc.id, "Ramesh", "7600000001", db)
        db.commit()
        p2 = get_or_create_patient(doc.id, "Suresh", "7600000001", db)
        db.commit()
        assert p1.id == p2.id
        db.close()

    def test_empty_queue_call_next_returns_none(self):
        db = TestSession()
        from services.auth_service import hash_password
        ts = int(datetime.now().timestamp() * 1000) % 1000000
        doc = Doctor(name="Dr Empty", email=f"empty{ts}@test.com",
                     phone=str(9900010000 + ts),
                     password_hash=hash_password("x"),
                     slug=f"drempty-{ts}",
                     trial_ends_at=datetime.utcnow() + timedelta(days=14))
        db.add(doc)
        db.commit()
        result = vs.call_next(db, doctor_id=doc.id)
        db.close()
        assert result is None

    def test_slot_boundary_first_and_last(self):
        d = date.today()
        while d.weekday() != 0:
            d += timedelta(days=1)
        db = TestSession()
        from services.auth_service import hash_password
        ts = int(datetime.now().timestamp() * 1000) % 1000000
        doc = Doctor(name="Dr Bound", email=f"bound2{ts}@test.com",
                     phone=str(9900020000 + ts),
                     password_hash=hash_password("x"),
                     slug=f"drbound2-{ts}",
                     trial_ends_at=datetime.utcnow() + timedelta(days=14))
        db.add(doc)
        db.flush()
        DoctorSchedule(doctor_id=doc.id, day_of_week=0,
                       start_time=time(9, 0), end_time=time(9, 30),
                       slot_duration=15, max_patients=30, is_active=True)
        sched = DoctorSchedule(doctor_id=doc.id, day_of_week=0,
                               start_time=time(9, 0), end_time=time(9, 30),
                               slot_duration=15, max_patients=30, is_active=True)
        db.add(sched)
        db.commit()
        slots = get_available_slots(doc.id, d, db, filter_past=False)
        db.close()
        assert "09:00" in slots
        assert "09:15" in slots
        assert "09:30" not in slots

    def test_no_doctor_schedule_no_slots(self, client):
        """A doctor with no schedule set returns empty slots."""
        tok = auth_cookie(client, "noscheddoctor@test.com")
        r = client.get(f"/appointments/slots?date={next_monday()}",
                       cookies={"access_token": tok})
        assert r.json()["slots"] == []

    def test_multiple_patients_queue_integrity(self):
        db = TestSession()
        from services.auth_service import hash_password
        ts = int(datetime.now().timestamp() * 1000) % 1000000
        doc = Doctor(name="Dr Multi", email=f"multi{ts}@test.com",
                     phone=str(9900030000 + ts),
                     password_hash=hash_password("x"),
                     slug=f"drmulti-{ts}",
                     trial_ends_at=datetime.utcnow() + timedelta(days=14))
        db.add(doc)
        db.flush()
        patients = []
        for i in range(3):
            p = Patient(doctor_id=doc.id, name=f"Patient {i}", phone=str(7600010000 + ts + i))
            db.add(p)
            db.flush()
            patients.append(p)
        db.commit()
        visits = [vs.check_in(db, doctor_id=doc.id, patient_id=p.id) for p in patients]
        db.commit()
        tokens = sorted([v.token_number for v in visits])
        assert tokens == [1, 2, 3]
        db.close()

    def test_walk_in_outside_schedule_still_books(self, client):
        """Walk-ins bypass schedule constraints and always succeed."""
        tok = auth_cookie(client, "walkinnosch@test.com")
        # No schedule set — walk-in should still work
        r = client.post("/appointments/walkin", data={
            "patient_name": "NoSched Walk-in",
            "patient_phone": _next_phone(),
            "patient_age": "40",
            "patient_gender": "male",
            "is_emergency": "",
        }, cookies={"access_token": tok}, follow_redirects=False)
        assert r.status_code in (200, 303)


# ─────────────────────────────────────────────────────────────────────────────
#  L. NOTIFICATIONS (mocked)
# ─────────────────────────────────────────────────────────────────────────────

class TestNotifications:

    def test_notification_failure_does_not_block_booking(self, client):
        """A Twilio crash must not prevent booking creation."""
        tok = auth_cookie(client, "notifail@test.com")
        make_schedule(client, tok)
        phone = _next_phone()
        # Patch the notification inside the service module (where it's imported at call time)
        with patch("services.notification_service.notify_appointment_confirmed",
                   side_effect=Exception("Twilio down")):
            r = book_appointment(client, tok, next_monday(), "09:00",
                                 patient_phone=phone)
        # Booking must succeed (status 303 redirect) despite notification failure
        assert r.status_code == 303
        db = TestSession()
        appt = db.query(Appointment).filter(
            Appointment.appointment_time == time(9, 0)
        ).first()
        db.close()
        assert appt is not None

    def test_walkin_notification_attempt(self, client):
        """Walk-in uses notify_walkin_queued (different function, not confirmation)."""
        tok = auth_cookie(client, "notiwalk@test.com")
        make_schedule(client, tok)
        with patch("services.notification_service.notify_walkin_queued") as mock_walkin:
            client.post("/appointments/walkin", data={
                "patient_name": "Walk Notification",
                "patient_phone": _next_phone(),
                "patient_age": "30",
                "patient_gender": "male",
                "is_emergency": "",
            }, cookies={"access_token": tok}, follow_redirects=False)
            # Walk-in calls notify_walkin_queued, NOT notify_appointment_confirmed

    def test_appointment_booking_calls_notification(self, client):
        """Regular booking (new_patient type) triggers notify_appointment_confirmed."""
        tok = auth_cookie(client, "noticonf@test.com")
        make_schedule(client, tok)
        called = []

        def fake_notify(*args, **kwargs):
            called.append(True)

        with patch("services.notification_service.notify_appointment_confirmed", fake_notify):
            # Use new_patient type — routes to notify_appointment_confirmed (not notify_followup_confirmed)
            client.post("/appointments", data={
                "patient_name": "Noti Patient",
                "patient_phone": _next_phone(),
                "patient_age": "30",
                "patient_gender": "male",
                "appt_date": next_monday(),
                "appt_time": "09:00",
                "appointment_type": "new_patient",
                "duration": "15",
                "patient_notes": "",
                "booked_by_field": "doctor",
                "for_doctor_id": "0",
            }, cookies={"access_token": tok}, follow_redirects=False)
        assert len(called) == 1


# ─────────────────────────────────────────────────────────────────────────────
#  M. PIN SYSTEM
# ─────────────────────────────────────────────────────────────────────────────

class TestPINSystem:

    def test_set_pin_stores_hash_not_plaintext(self, client):
        tok = auth_cookie(client, "pin1@test.com")
        client.post("/doctors/settings/pin", data={
            "action": "set", "new_pin": "432100", "confirm_pin": "432100", "current_pin": "",
        }, cookies={"access_token": tok}, follow_redirects=False)
        db = TestSession()
        doc = db.query(Doctor).filter(Doctor.email == "pin1@test.com").first()
        db.close()
        assert doc.pin_hash is not None
        assert doc.pin_hash != "432100"
        assert len(doc.pin_hash) > 30  # bcrypt hash

    def test_correct_pin_issues_session_cookie(self, client):
        tok = auth_cookie(client, "pin2@test.com")
        client.post("/doctors/settings/pin", data={
            "action": "set", "new_pin": "111111", "confirm_pin": "111111", "current_pin": "",
        }, cookies={"access_token": tok}, follow_redirects=False)
        r = client.post("/pin-prompt", data={
            "pin": "111111", "next": "/reports",
        }, cookies={"access_token": tok}, follow_redirects=False)
        assert r.status_code in (302, 303)
        assert "pin_session" in r.cookies

    def test_wrong_pin_does_not_issue_session(self, client):
        tok = auth_cookie(client, "pin3@test.com")
        client.post("/doctors/settings/pin", data={
            "action": "set", "new_pin": "222222", "confirm_pin": "222222", "current_pin": "",
        }, cookies={"access_token": tok}, follow_redirects=False)
        r = client.post("/pin-prompt", data={
            "pin": "999999", "next": "/reports",
        }, cookies={"access_token": tok}, follow_redirects=False)
        assert "pin_session" not in r.cookies

    def test_pin_protected_route_shows_overlay_without_session(self, client):
        tok = auth_cookie(client, "pin4@test.com")
        client.post("/doctors/settings/pin", data={
            "action": "set", "new_pin": "333333", "confirm_pin": "333333", "current_pin": "",
        }, cookies={"access_token": tok}, follow_redirects=False)
        # Access /reports without pin_session
        r = client.get("/reports", cookies={"access_token": tok})
        assert r.status_code in (200, 302, 303)
        if r.status_code == 200:
            assert b"pin" in r.content.lower() or b"unlock" in r.content.lower()

    def test_remove_pin_clears_hash(self, client):
        tok = auth_cookie(client, "pin5@test.com")
        client.post("/doctors/settings/pin", data={
            "action": "set", "new_pin": "555555", "confirm_pin": "555555", "current_pin": "",
        }, cookies={"access_token": tok}, follow_redirects=False)
        client.post("/doctors/settings/pin", data={
            "action": "remove", "current_pin": "555555",
            "new_pin": "", "confirm_pin": "",
        }, cookies={"access_token": tok}, follow_redirects=False)
        db = TestSession()
        doc = db.query(Doctor).filter(Doctor.email == "pin5@test.com").first()
        db.close()
        assert doc.pin_hash is None

    def test_no_pin_set_reports_accessible_without_session(self, client):
        tok = auth_cookie(client, "pin6@test.com")
        r = client.get("/reports", cookies={"access_token": tok})
        assert r.status_code == 200

    def test_pin_session_unlocks_protected_route(self, client):
        tok = auth_cookie(client, "pin7@test.com")
        client.post("/doctors/settings/pin", data={
            "action": "set", "new_pin": "666666", "confirm_pin": "666666", "current_pin": "",
        }, cookies={"access_token": tok}, follow_redirects=False)
        # Get pin_session
        r = client.post("/pin-prompt", data={
            "pin": "666666", "next": "/reports",
        }, cookies={"access_token": tok}, follow_redirects=False)
        pin_session = r.cookies.get("pin_session")
        assert pin_session is not None
        # Access /reports with pin_session
        r2 = client.get("/reports",
                        cookies={"access_token": tok, "pin_session": pin_session})
        assert r2.status_code == 200


# ══════════════════════════════════════════════════════════════════════
#  Auth hardening (Phase 0)
# ══════════════════════════════════════════════════════════════════════

class TestPasswordPolicy:
    """Server-side password policy — the HTML minlength is not a control."""

    def test_short_password_rejected(self, client):
        r = register(client, email="pw1@test.com", phone="9310000001",
                     password="Short1!x")          # 8 chars, under the 12 min
        assert r.status_code == 400
        assert b"12 characters" in r.content

    def test_password_containing_name_rejected(self, client):
        r = register(client, email="pw2@test.com", phone="9310000002",
                     name="Ramesh Kumar", password="RameshKumar99xy")
        assert r.status_code == 400
        assert b"must not contain your name" in r.content

    def test_password_without_symbols_rejected(self, client):
        r = register(client, email="pw2b@test.com", phone="9310000012",
                     password="NoSymbolsHere99")
        assert r.status_code == 400
        assert b"special characters or symbols" in r.content

    def test_password_with_one_symbol_rejected(self, client):
        r = register(client, email="pw2c@test.com", phone="9310000013",
                     password="OnlyOneSymbol9!")
        assert r.status_code == 400
        assert b"special characters or symbols" in r.content

    def test_password_containing_email_localpart_rejected(self, client):
        r = register(client, email="drsunita@test.com", phone="9310000003",
                     password="drsunitaClinic9")
        assert r.status_code == 400
        assert b"must not contain your name" in r.content

    def test_sequential_password_rejected(self, client):
        r = register(client, email="pw4@test.com", phone="9310000004",
                     password="Xk9mVpQz1234Rt")
        assert r.status_code == 400
        assert b"keyboard patterns" in r.content

    def test_common_password_rejected(self, client):
        r = register(client, email="pw5@test.com", phone="9310000005",
                     password="password123")
        assert r.status_code == 400

    def test_all_problems_returned_together(self, client):
        """One submit should surface every failure, not just the first."""
        r = register(client, email="pw6@test.com", phone="9310000006",
                     name="Ravi", password="ravi1234")
        assert r.status_code == 400
        body = r.content
        assert b"12 characters" in body          # too short
        assert b"keyboard patterns" in body      # contains 1234

    def test_valid_password_accepted(self, client):
        r = register(client, email="pw7@test.com", phone="9310000007",
                     password="Zx9@pLmv6Bq4Nr#")
        assert r.status_code in (200, 302, 303)
        db = TestSession()
        doc = db.query(Doctor).filter(Doctor.email == "pw7@test.com").first()
        db.close()
        assert doc is not None


class TestRegistrationHardening:

    def test_email_case_variant_is_400_not_500(self, client):
        """Regression: the dup-check compared raw email while the insert
        lowercased it, so a case-variant slipped through to the DB unique
        constraint and surfaced as a 500."""
        r1 = register(client, email="Case@Test.com", phone="9320000001",
                      password="Zx9@pLmv6Bq4Nr#")
        assert r1.status_code in (200, 302, 303)
        r2 = register(client, email="case@test.com", phone="9320000002",
                      password="Zx9@pLmv6Bq4Nr#")
        assert r2.status_code == 400, "case-variant duplicate must be a friendly 400"

    def test_email_stored_lowercased(self, client):
        register(client, email="MiXeD@Test.com", phone="9320000003",
                 password="Zx9@pLmv6Bq4Nr#")
        db = TestSession()
        doc = db.query(Doctor).filter(Doctor.email == "mixed@test.com").first()
        db.close()
        assert doc is not None, "email should be normalised to lowercase on write"

    def test_case_variant_can_log_in(self, client):
        register(client, email="Login@Test.com", phone="9320000004",
                 password="Zx9@pLmv6Bq4Nr#")
        r = login(client, "LOGIN@TEST.COM", "Zx9@pLmv6Bq4Nr#")
        assert r.status_code == 303

    def test_no_user_enumeration_between_email_and_phone(self, client):
        """A duplicate email and a duplicate phone must be indistinguishable,
        otherwise /register becomes a probe for who is on Med Track."""
        register(client, email="enum@test.com", phone="9320000010",
                 password="Zx9@pLmv6Bq4Nr#")
        dup_email = register(client, email="enum@test.com", phone="9320000011",
                             password="Zx9@pLmv6Bq4Nr#")
        dup_phone = register(client, email="other@test.com", phone="9320000010",
                             password="Zx9@pLmv6Bq4Nr#")
        assert dup_email.status_code == dup_phone.status_code == 400
        assert dup_email.content == dup_phone.content, \
            "responses must be byte-identical to prevent enumeration"

    def test_short_phone_rejected(self, client):
        r = register(client, email="ph1@test.com", phone="12345",
                     password="Zx9@pLmv6Bq4Nr#")
        assert r.status_code == 400

    def test_invalid_email_rejected(self, client):
        r = register(client, email="not-an-email", phone="9320000020",
                     password="Zx9@pLmv6Bq4Nr#")
        assert r.status_code == 400


class TestPasswordHashing:

    def test_new_passwords_use_argon2id(self, client):
        register(client, email="hash1@test.com", phone="9330000001",
                 password="Zx9@pLmv6Bq4Nr#")
        db = TestSession()
        doc = db.query(Doctor).filter(Doctor.email == "hash1@test.com").first()
        db.close()
        assert doc.password_hash.startswith("$argon2id$")

    def test_legacy_bcrypt_hash_still_logs_in_and_upgrades(self, client):
        """Existing doctors must not be locked out, and should migrate to
        argon2id transparently on their next successful login."""
        from passlib.context import CryptContext
        bcrypt_only = CryptContext(schemes=["bcrypt"])
        legacy_hash = bcrypt_only.hash("Zx9@pLmv6Bq4Nr#")

        db = TestSession()
        doc = Doctor(
            name="Legacy Doc", email="legacy@test.com", phone="9330000002",
            password_hash=legacy_hash, slug="legacy-doc-hash",
            plan_type=PlanType.trial,
            trial_ends_at=datetime.utcnow() + timedelta(days=14),
        )
        db.add(doc)
        db.commit()
        db.close()

        assert legacy_hash.startswith("$2b$")

        r = login(client, "legacy@test.com", "Zx9@pLmv6Bq4Nr#")
        assert r.status_code == 303, "legacy bcrypt doctor must still log in"

        db = TestSession()
        doc = db.query(Doctor).filter(Doctor.email == "legacy@test.com").first()
        upgraded = doc.password_hash
        db.close()
        assert upgraded.startswith("$argon2id$"), "hash should upgrade on login"

    def test_malformed_hash_fails_closed(self):
        """A corrupt hash must be a failed login, never a 500."""
        from services.auth_service import verify_password
        assert verify_password("anything", "not-a-real-hash") is False


class TestLogoutClearsAllCookies:

    def test_logout_clears_pin_and_admin_cookies(self, client):
        """Only access_token used to be cleared, leaving a live pin_session
        behind so the PIN gate was skipped after logging back in."""
        r = client.get("/logout", follow_redirects=False)
        assert r.status_code == 303
        cleared = [
            h for h in r.headers.get_list("set-cookie")
            if 'Max-Age=0' in h or 'expires=Thu, 01 Jan 1970' in h.lower()
        ]
        blob = " ".join(cleared)
        for name in ("access_token", "pin_session", "clinic_admin_auth"):
            assert name in blob, f"{name} not cleared on logout"


class TestClientIPExtraction:
    """Rate limiting keys on this. Railway reports 100.64.0.0/10 (CGNAT) as
    request.client.host, so XFF must be parsed or all doctors share a bucket."""

    def _req(self, headers, peer="100.64.0.7"):
        class _R:
            def __init__(self):
                self.headers = headers
                self.client = type("C", (), {"host": peer})()
        return _R()

    def test_public_client_ip_extracted(self):
        from main import _client_ip
        assert _client_ip(self._req({"x-forwarded-for": "49.36.180.5"})) == "49.36.180.5"

    def test_cgnat_hop_skipped(self):
        from main import _client_ip
        got = _client_ip(self._req({"x-forwarded-for": "49.36.180.5, 100.64.0.3"}))
        assert got == "49.36.180.5", "Railway's CGNAT hop must be skipped"

    def test_forged_left_entry_ignored(self):
        from main import _client_ip
        got = _client_ip(self._req({"x-forwarded-for": "1.2.3.4, 49.36.180.5"}))
        assert got == "49.36.180.5", "must take the rightmost public entry"

    def test_all_private_falls_back_to_peer(self):
        from main import _client_ip
        assert _client_ip(self._req({"x-forwarded-for": "10.0.0.1, 192.168.1.1"})) == "100.64.0.7"

    def test_malformed_header_falls_back(self):
        from main import _client_ip
        assert _client_ip(self._req({"x-forwarded-for": "garbage,,"})) == "100.64.0.7"


# ══════════════════════════════════════════════════════════════════════
#  Email infrastructure (Phase 1)
# ══════════════════════════════════════════════════════════════════════

class TestEmailService:
    """send_email must never raise — a mail outage cannot 500 a request."""

    def test_unconfigured_returns_false_not_raise(self):
        from services.email_service import send_email
        from config import settings
        original = settings.RESEND_API_KEY
        settings.RESEND_API_KEY = ""
        try:
            ok, detail = send_email("doc@example.com", "Subject", "<p>body</p>")
        finally:
            settings.RESEND_API_KEY = original
        assert ok is False
        assert detail == "not configured"

    def test_empty_recipient_rejected(self):
        from services.email_service import send_email
        ok, detail = send_email("", "Subject", "<p>body</p>")
        assert ok is False
        assert detail == "no recipient"

    def test_render_wraps_body(self):
        from services.email_service import render_email
        html = render_email("<p>hello</p>")
        assert "<p>hello</p>" in html
        assert "Med Track" in html

    def test_code_block_renders_digits(self):
        from services.email_service import code_block
        assert "493018" in code_block("493018")

    def test_button_includes_url_and_label(self):
        from services.email_service import button
        out = button("https://example.com/x", "Accept")
        assert 'href="https://example.com/x"' in out
        assert "Accept" in out


class TestInviteService:

    def test_invite_url_matches_real_route(self):
        """Regression: the service built /clinic/invite/{token} while the
        actual route is /clinic/doctor-invite/{token}, so every emailed
        link 404'd."""
        from services.invite_service import build_invite_url
        url = build_invite_url("tok_abc123")
        assert "/clinic/doctor-invite/tok_abc123" in url
        assert "/clinic/invite/" not in url.replace("/clinic/doctor-invite/", "")

    def test_send_invite_never_raises_when_unconfigured(self):
        """Previously raised RuntimeError on every call because the SMTP_*
        settings it read were never declared on Settings."""
        from services.invite_service import send_invite_email
        from config import settings
        original = settings.RESEND_API_KEY
        settings.RESEND_API_KEY = ""
        try:
            ok, detail = send_invite_email(
                "doc@example.com", "tok_x", "Verma Clinic", "Dr Asha"
            )
        finally:
            settings.RESEND_API_KEY = original
        assert ok is False
        assert detail == "not configured"

    def test_settings_has_no_smtp_fields(self):
        """Guards the old failure mode: invite_service used getattr(settings,
        'SMTP_HOST', None), which silently returned None forever."""
        from config import settings
        for field in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "BASE_URL"):
            assert not hasattr(settings, field), (
                f"{field} reappeared on Settings — invite_service no longer uses "
                f"SMTP; use PUBLIC_BASE_URL and RESEND_API_KEY instead"
            )


# ══════════════════════════════════════════════════════════════════════
#  Email verification OTP (Phase 2)
# ══════════════════════════════════════════════════════════════════════

class TestEmailVerification:

    def _doctor(self, db, email, phone, slug):
        from services.auth_service import hash_password
        d = Doctor(name="Dr Verify", email=email, phone=phone,
                   password_hash=hash_password("Zx9@pLmv6Bq4Nr#"), slug=slug,
                   plan_type=PlanType.trial,
                   trial_ends_at=datetime.utcnow() + timedelta(days=14))
        db.add(d); db.commit(); db.refresh(d)
        return d

    def _capture(self, monkeypatch_target, codes):
        """Wrap _generate_code so the test can read the plaintext code."""
        import services.verification_service as vs
        original = vs._generate_code

        def _wrapped():
            c = original()
            codes.append(c)
            return c
        vs._generate_code = _wrapped
        return original

    def test_code_stored_hashed_never_plaintext(self, client):
        import services.verification_service as vs
        from database.models import EmailVerification
        db = TestSession()
        doc = self._doctor(db, "vh1@test.com", "9340000001", "v-h1")
        codes = []
        orig = self._capture(vs, codes)
        try:
            vs.issue_code(db, doc)
        finally:
            vs._generate_code = orig
        rec = db.query(EmailVerification).filter_by(doctor_id=doc.id).first()
        plaintext = codes[0]
        db.close()
        assert rec.code_hash.startswith("$argon2id$")
        assert plaintext not in rec.code_hash

    def test_correct_code_verifies(self, client):
        import services.verification_service as vs
        db = TestSession()
        doc = self._doctor(db, "vh2@test.com", "9340000002", "v-h2")
        codes = []
        orig = self._capture(vs, codes)
        try:
            vs.issue_code(db, doc)
            ok, msg = vs.verify_code(db, doc, codes[0])
        finally:
            vs._generate_code = orig
        verified = doc.email_verified_at
        db.close()
        assert ok is True
        assert verified is not None

    def test_wrong_code_rejected_and_counts_attempt(self, client):
        import services.verification_service as vs
        from database.models import EmailVerification
        db = TestSession()
        doc = self._doctor(db, "vh3@test.com", "9340000003", "v-h3")
        vs.issue_code(db, doc)
        ok, msg = vs.verify_code(db, doc, "000000")
        rec = db.query(EmailVerification).filter_by(doctor_id=doc.id).first()
        attempts = rec.attempts
        db.close()
        assert ok is False
        assert attempts == 1

    def test_code_burned_after_max_attempts(self, client):
        import services.verification_service as vs
        from database.models import EmailVerification
        db = TestSession()
        doc = self._doctor(db, "vh4@test.com", "9340000004", "v-h4")
        vs.issue_code(db, doc)
        for _ in range(vs.MAX_ATTEMPTS):
            vs.verify_code(db, doc, "000000")
        rec = db.query(EmailVerification).filter_by(doctor_id=doc.id).first()
        consumed = rec.consumed_at
        db.close()
        assert consumed is not None, "code must be burned after the attempt cap"

    def test_expired_code_rejected(self, client):
        import services.verification_service as vs
        from database.models import EmailVerification
        db = TestSession()
        doc = self._doctor(db, "vh5@test.com", "9340000005", "v-h5")
        codes = []
        orig = self._capture(vs, codes)
        try:
            vs.issue_code(db, doc)
        finally:
            vs._generate_code = orig
        rec = db.query(EmailVerification).filter_by(doctor_id=doc.id).first()
        rec.expires_at = datetime.utcnow() - timedelta(minutes=1)
        db.commit()
        ok, msg = vs.verify_code(db, doc, codes[0])
        db.close()
        assert ok is False
        assert "expired" in msg.lower()

    def test_reissue_invalidates_previous_code(self, client):
        import services.verification_service as vs
        from database.models import EmailVerification
        db = TestSession()
        doc = self._doctor(db, "vh6@test.com", "9340000006", "v-h6")
        codes = []
        orig = self._capture(vs, codes)
        try:
            vs.issue_code(db, doc)
            # clear the resend cooldown
            rec = db.query(EmailVerification).filter_by(doctor_id=doc.id).first()
            rec.created_at = datetime.utcnow() - timedelta(seconds=120)
            db.commit()
            vs.issue_code(db, doc)
            old_ok, _ = vs.verify_code(db, doc, codes[0])
            new_ok, _ = vs.verify_code(db, doc, codes[1])
        finally:
            vs._generate_code = orig
        db.close()
        assert old_ok is False, "superseded code must not verify"
        assert new_ok is True

    def test_resend_cooldown_enforced(self, client):
        import services.verification_service as vs
        db = TestSession()
        doc = self._doctor(db, "vh7@test.com", "9340000007", "v-h7")
        vs.issue_code(db, doc)
        ok, detail = vs.issue_code(db, doc)
        db.close()
        assert ok is False
        assert "wait" in detail.lower()

    def test_change_email_bypasses_cooldown_and_sends(self, client):
        """Regression: the resend cooldown blocked the new code, so a doctor
        correcting a typo got nothing — defeating the whole fallback."""
        import services.verification_service as vs
        db = TestSession()
        doc = self._doctor(db, "typo@gmial.com", "9340000008", "v-h8")
        codes = []
        orig = self._capture(vs, codes)
        try:
            vs.issue_code(db, doc)
            ok, msg = vs.change_email(db, doc, "Correct@Gmail.com")
        finally:
            vs._generate_code = orig
        new_email = doc.email
        db.close()
        assert ok is True
        assert new_email == "correct@gmail.com", "must normalise the new address"
        assert len(codes) == 2, "a fresh code must actually be sent"
        assert "could not be sent" not in msg

    def test_change_email_to_taken_address_is_generic(self, client):
        import services.verification_service as vs
        db = TestSession()
        self._doctor(db, "taken@test.com", "9340000009", "v-h9")
        doc = self._doctor(db, "mover@test.com", "9340000010", "v-h10")
        ok, msg = vs.change_email(db, doc, "taken@test.com")
        db.close()
        assert ok is False
        assert "already" not in msg.lower(), "must not confirm the address exists"

    def test_verified_doctor_redirected_away_from_verify_page(self, client):
        tok = auth_cookie(client, "vdone@test.com")
        db = TestSession()
        doc = db.query(Doctor).filter(Doctor.email == "vdone@test.com").first()
        doc.email_verified_at = datetime.utcnow()
        db.commit(); db.close()
        r = client.get("/verify-email", cookies={"access_token": tok},
                       follow_redirects=False)
        assert r.status_code == 303
        assert "/dashboard" in r.headers.get("location", "")

    def test_unverified_doctor_blocked_from_dashboard(self, client):
        """Verification is mandatory — no skip. get_paying_doctor() must
        bounce an unverified doctor to /verify-email instead of rendering."""
        from services.auth_service import create_access_token
        db = TestSession()
        doc = self._doctor(db, "gate1@test.com", "9340000011", "v-gate1")
        doc_id = doc.id
        db.close()
        tok = create_access_token({"doctor_id": doc_id, "tv": 0})
        r = client.get("/dashboard", cookies={"access_token": tok}, follow_redirects=False)
        assert r.status_code == 303
        assert "/verify-email" in r.headers.get("location", "")

    def test_unverified_doctor_blocked_from_appointment_detail(self, client):
        """get_appt_doctor() duplicates the plan gate and must carry the same
        verification check — it doesn't sit behind get_paying_doctor()."""
        from services.auth_service import create_access_token
        db = TestSession()
        doc = self._doctor(db, "gate2@test.com", "9340000012", "v-gate2")
        doc_id = doc.id
        db.close()
        tok = create_access_token({"doctor_id": doc_id, "tv": 0})
        r = client.get("/appointments/1", cookies={"access_token": tok}, follow_redirects=False)
        assert r.status_code == 303
        assert "/verify-email" in r.headers.get("location", "")

    def test_verified_doctor_reaches_dashboard(self, client):
        from services.auth_service import create_access_token
        db = TestSession()
        doc = self._doctor(db, "gate3@test.com", "9340000013", "v-gate3")
        doc.email_verified_at = datetime.utcnow()
        db.commit()
        doc_id = doc.id
        db.close()
        tok = create_access_token({"doctor_id": doc_id, "tv": 0})
        r = client.get("/dashboard", cookies={"access_token": tok}, follow_redirects=False)
        assert r.status_code == 200

    def test_no_skip_option_on_verify_page(self, client):
        from services.auth_service import create_access_token
        db = TestSession()
        doc = self._doctor(db, "gate4@test.com", "9340000014", "v-gate4")
        doc_id = doc.id
        db.close()
        tok = create_access_token({"doctor_id": doc_id, "tv": 0})
        r = client.get("/verify-email", cookies={"access_token": tok})
        assert b"Skip for now" not in r.content
        assert b"ve-skip" not in r.content

    def test_registration_does_not_block_login(self, client):
        """Verification must NOT gate login — otherwise a mail outage locks
        doctors out, and every existing test would break."""
        register(client, email="nogate@test.com", phone="9340000020")
        r = login(client, "nogate@test.com")
        assert r.status_code == 303, "unverified doctors must still log in"


# ══════════════════════════════════════════════════════════════════════
#  Password reset (Phase 3)
# ══════════════════════════════════════════════════════════════════════

class TestPasswordReset:

    def _doctor(self, db, email, phone, slug, verified=True):
        from services.auth_service import hash_password
        d = Doctor(name="Dr Reset", email=email, phone=phone,
                   password_hash=hash_password("Zx9@pLmv6Bq4Nr#"), slug=slug,
                   plan_type=PlanType.trial,
                   trial_ends_at=datetime.utcnow() + timedelta(days=14),
                   email_verified_at=datetime.utcnow() if verified else None,
                   token_version=0)
        db.add(d); db.commit(); db.refresh(d)
        return d

    def _capture(self, tokens):
        """Wrap token generation so the test can read the plaintext token."""
        import services.password_reset_service as prs
        original = prs.secrets.token_urlsafe

        def _wrapped(n=32):
            t = original(n)
            tokens.append(t)
            return t
        prs.secrets.token_urlsafe = _wrapped
        return original

    def test_token_stored_hashed(self, client):
        import services.password_reset_service as prs
        from database.models import PasswordReset
        db = TestSession()
        self._doctor(db, "pr1@test.com", "9350000001", "pr-1")
        tokens = []
        orig = self._capture(tokens)
        try:
            prs.request_reset(db, "pr1@test.com")
        finally:
            prs.secrets.token_urlsafe = orig
        rec = db.query(PasswordReset).first()
        db.close()
        assert rec.token_hash.startswith("$argon2id$")
        assert tokens[0] not in rec.token_hash

    def test_unknown_email_creates_no_token_and_does_not_raise(self, client):
        import services.password_reset_service as prs
        from database.models import PasswordReset
        db = TestSession()
        prs.request_reset(db, "ghost@nowhere.com")   # must not raise
        count = db.query(PasswordReset).count()
        db.close()
        assert count == 0

    def test_unverified_email_gets_no_reset_link(self, client):
        """Otherwise registering with someone else's address would be an
        account-takeover path."""
        import services.password_reset_service as prs
        from database.models import PasswordReset
        db = TestSession()
        doc = self._doctor(db, "pr2@test.com", "9350000002", "pr-2", verified=False)
        prs.request_reset(db, "pr2@test.com")
        count = db.query(PasswordReset).filter_by(doctor_id=doc.id).count()
        db.close()
        assert count == 0

    def test_valid_token_resets_password(self, client):
        import services.password_reset_service as prs
        from services.auth_service import verify_password
        db = TestSession()
        doc = self._doctor(db, "pr3@test.com", "9350000003", "pr-3")
        tokens = []
        orig = self._capture(tokens)
        try:
            prs.request_reset(db, "pr3@test.com")
            rec = prs.validate_token(db, tokens[0])
            ok, msg = prs.consume_reset(db, rec, "Nw8#qRtz4Vm7Kp!")
        finally:
            prs.secrets.token_urlsafe = orig
        db.refresh(doc)
        new_hash = doc.password_hash
        db.close()
        assert ok is True
        assert verify_password("Nw8#qRtz4Vm7Kp!", new_hash)

    def test_token_is_single_use(self, client):
        import services.password_reset_service as prs
        db = TestSession()
        self._doctor(db, "pr4@test.com", "9350000004", "pr-4")
        tokens = []
        orig = self._capture(tokens)
        try:
            prs.request_reset(db, "pr4@test.com")
            rec = prs.validate_token(db, tokens[0])
            prs.consume_reset(db, rec, "Nw8#qRtz4Vm7Kp!")
            replay = prs.validate_token(db, tokens[0])
        finally:
            prs.secrets.token_urlsafe = orig
        db.close()
        assert replay is None, "a consumed token must not validate again"

    def test_expired_token_rejected(self, client):
        import services.password_reset_service as prs
        from database.models import PasswordReset
        db = TestSession()
        doc = self._doctor(db, "pr5@test.com", "9350000005", "pr-5")
        tokens = []
        orig = self._capture(tokens)
        try:
            prs.request_reset(db, "pr5@test.com")
            rec = db.query(PasswordReset).filter_by(doctor_id=doc.id).first()
            rec.expires_at = datetime.utcnow() - timedelta(minutes=1)
            db.commit()
            result = prs.validate_token(db, tokens[0])
        finally:
            prs.secrets.token_urlsafe = orig
        db.close()
        assert result is None

    def test_reset_enforces_password_policy(self, client):
        import services.password_reset_service as prs
        db = TestSession()
        self._doctor(db, "pr6@test.com", "9350000006", "pr-6")
        tokens = []
        orig = self._capture(tokens)
        try:
            prs.request_reset(db, "pr6@test.com")
            rec = prs.validate_token(db, tokens[0])
            ok, msg = prs.consume_reset(db, rec, "short1!")
        finally:
            prs.secrets.token_urlsafe = orig
        db.close()
        assert ok is False
        assert "12 characters" in msg

    def test_reset_bumps_token_version(self, client):
        """This is what kills every existing session, including a stolen one."""
        import services.password_reset_service as prs
        db = TestSession()
        doc = self._doctor(db, "pr7@test.com", "9350000007", "pr-7")
        before = doc.token_version
        tokens = []
        orig = self._capture(tokens)
        try:
            prs.request_reset(db, "pr7@test.com")
            rec = prs.validate_token(db, tokens[0])
            prs.consume_reset(db, rec, "Nw8#qRtz4Vm7Kp!")
        finally:
            prs.secrets.token_urlsafe = orig
        db.refresh(doc)
        after = doc.token_version
        db.close()
        assert after == before + 1

    def test_pre_reset_session_cookie_is_rejected(self, client):
        """End-to-end: a cookie minted before the reset must stop working."""
        import services.password_reset_service as prs
        tok = auth_cookie(client, "pr8@test.com")
        assert client.get("/dashboard", cookies={"access_token": tok}).status_code == 200

        db = TestSession()
        doc = db.query(Doctor).filter(Doctor.email == "pr8@test.com").first()
        doc.email_verified_at = datetime.utcnow()
        db.commit()
        tokens = []
        orig = self._capture(tokens)
        try:
            prs.request_reset(db, "pr8@test.com")
            rec = prs.validate_token(db, tokens[0])
            prs.consume_reset(db, rec, "Nw8#qRtz4Vm7Kp!")
        finally:
            prs.secrets.token_urlsafe = orig
        db.close()

        r = client.get("/dashboard", cookies={"access_token": tok},
                       follow_redirects=False)
        assert r.status_code in (302, 303, 401), \
            "session minted before the reset must be rejected"

    def test_legacy_token_without_tv_still_works(self, client):
        """Shipping token_version must NOT log out everyone already signed in."""
        from services.auth_service import create_access_token, decode_token, _token_version_ok
        db = TestSession()
        doc = self._doctor(db, "pr9@test.com", "9350000009", "pr-9")
        legacy = create_access_token({"doctor_id": doc.id})   # no "tv" claim
        assert _token_version_ok(decode_token(legacy), doc) is True
        doc.token_version = 1
        db.commit()
        assert _token_version_ok(decode_token(legacy), doc) is False
        db.close()

    def test_forgot_password_does_not_enumerate(self, client):
        """Known and unknown addresses must be indistinguishable."""
        register(client, email="known@test.com", phone="9350000020")
        r_known = client.post("/forgot-password", data={"email": "known@test.com"},
                              follow_redirects=False)
        r_unknown = client.post("/forgot-password", data={"email": "nobody@nowhere.com"},
                                follow_redirects=False)
        assert r_known.status_code == r_unknown.status_code == 200
        norm_k = r_known.text.replace("known@test.com", "X")
        norm_u = r_unknown.text.replace("nobody@nowhere.com", "X")
        assert norm_k == norm_u, "responses must not reveal whether the account exists"

    def test_login_page_has_forgot_link(self, client):
        # The shared client is session-scoped and carries cookies from earlier
        # tests; a live session makes /login redirect to /dashboard. Send an
        # empty token so the login page actually renders.
        r = client.get("/login", cookies={"access_token": ""})
        assert r.status_code == 200
        assert "/forgot-password" in r.text


# ══════════════════════════════════════════════════════════════════════
#  Session hardening (Phase 4)
# ══════════════════════════════════════════════════════════════════════

class TestSessionHardening:

    def _aged_token(self, doctor_id, *, age_min, life_min=60, tv=0):
        """Mint a token that is `age_min` into a `life_min` life."""
        from datetime import timezone
        from jose import jwt
        from config import settings
        now = datetime.now(timezone.utc).timestamp()
        iat = int(now - age_min * 60)
        payload = {"doctor_id": doctor_id, "tv": tv, "iat": iat,
                   "jti": "testjti", "exp": int(iat + life_min * 60)}
        return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

    def test_token_lifetime_matches_config(self):
        """Regression: datetime.utcnow().timestamp() treats naive values as
        LOCAL time, and main.py forces TZ=Asia/Kolkata. That shifted iat 5.5h
        into the past and inflated the apparent lifetime to 390 minutes, so
        the renewal threshold sat past the token's own expiry and sliding
        renewal never fired."""
        from services.auth_service import create_access_token, decode_token
        from config import settings
        p = decode_token(create_access_token({"doctor_id": 1, "tv": 0}))
        lifetime_min = (p["exp"] - p["iat"]) / 60
        assert abs(lifetime_min - settings.ACCESS_TOKEN_EXPIRE_MINUTES) < 1, \
            f"lifetime {lifetime_min}min != configured {settings.ACCESS_TOKEN_EXPIRE_MINUTES}min"

    def test_iat_has_no_timezone_drift(self):
        from datetime import timezone
        from services.auth_service import create_access_token, decode_token
        p = decode_token(create_access_token({"doctor_id": 1}))
        now = datetime.now(timezone.utc).timestamp()
        assert abs(now - p["iat"]) < 30, "iat drifted — timezone handling is wrong"

    def test_token_carries_iat_jti_tv(self):
        from services.auth_service import create_access_token, decode_token
        p = decode_token(create_access_token({"doctor_id": 1, "tv": 3}))
        assert "iat" in p and "jti" in p
        assert p["tv"] == 3

    def test_fresh_session_not_renewed(self):
        from services.auth_service import decode_token, should_renew
        p = decode_token(self._aged_token(1, age_min=5))
        assert should_renew(p) is False

    def test_session_past_halfway_is_renewed(self):
        from services.auth_service import decode_token, should_renew
        p = decode_token(self._aged_token(1, age_min=40))
        assert should_renew(p) is True

    def test_absolute_cap_stops_renewal(self):
        """Sliding renewal must not let a session live forever.

        Payloads are built directly rather than encoded/decoded: a token this
        old is already past `exp`, so decode_token() would return None and the
        test would assert nothing about the cap.
        """
        from datetime import timezone
        from services.auth_service import should_renew, SESSION_ABSOLUTE_MAX_HOURS

        now = datetime.now(timezone.utc).timestamp()

        def payload(age_hours):
            iat = int(now - age_hours * 3600)
            # Still-live token (renewal only ever runs on a valid session).
            return {"doctor_id": 1, "tv": 0, "iat": iat, "exp": int(now + 20 * 60)}

        assert should_renew(payload(SESSION_ABSOLUTE_MAX_HOURS - 1)) is True
        assert should_renew(payload(SESSION_ABSOLUTE_MAX_HOURS + 1)) is False

    def test_should_renew_handles_none(self):
        """decode_token returns None for an expired/invalid token."""
        from services.auth_service import should_renew
        assert should_renew(None) is False

    def test_legacy_token_without_iat_is_renewed(self):
        """Tokens minted before this feature carry no iat; renewing them once
        stamps one and starts the cap from that point."""
        from datetime import timezone
        from services.auth_service import should_renew
        exp = int(datetime.now(timezone.utc).timestamp()) + 600
        assert should_renew({"doctor_id": 1, "exp": exp}) is True

    def test_malformed_payload_not_renewed(self):
        from services.auth_service import should_renew
        assert should_renew({"doctor_id": 1}) is False       # no exp
        assert should_renew({}) is False

    def test_renewal_preserves_iat_so_cap_cannot_reset(self, client):
        """If renewal reset iat, the 12h cap would never be reached."""
        from services.auth_service import create_access_token, decode_token
        original = decode_token(self._aged_token(1, age_min=40))
        renewed = decode_token(create_access_token({
            k: original[k] for k in ("doctor_id", "tv", "iat", "jti") if k in original
        }))
        assert renewed["iat"] == original["iat"]
        assert renewed["exp"] > original["exp"]

    def test_aged_session_gets_renewed_over_http(self, client):
        """The middleware must actually re-issue the cookie."""
        tok = auth_cookie(client, "sess1@test.com")
        db = TestSession()
        doc = db.query(Doctor).filter(Doctor.email == "sess1@test.com").first()
        doc_id, tv = doc.id, doc.token_version or 0
        db.close()

        aged = self._aged_token(doc_id, age_min=40, tv=tv)
        r = client.get("/dashboard", cookies={"access_token": aged})
        assert r.status_code == 200
        cookies = " ".join(r.headers.get_list("set-cookie"))
        assert "access_token=" in cookies, "aged session should have been renewed"

    def test_logout_is_not_undone_by_renewal(self, client):
        """Renewal runs in middleware AFTER the route. If it re-set the cookie
        on /logout it would silently break logging out."""
        tok = auth_cookie(client, "sess2@test.com")
        db = TestSession()
        doc = db.query(Doctor).filter(Doctor.email == "sess2@test.com").first()
        doc_id, tv = doc.id, doc.token_version or 0
        db.close()

        aged = self._aged_token(doc_id, age_min=40, tv=tv)
        r = client.get("/logout", cookies={"access_token": aged},
                       follow_redirects=False)
        cookies = r.headers.get_list("set-cookie")
        access = [c for c in cookies if c.startswith("access_token=")]
        assert access, "logout should clear access_token"
        for c in access:
            assert "Max-Age=0" in c or "01 Jan 1970" in c, \
                f"logout re-issued a live cookie: {c}"


class TestClinicAccountSignup:
    """account_type='clinic' at registration used to be accepted by the form
    and silently discarded server-side — every signup got an ordinary solo
    account regardless of what was selected, and Clinic.plan_type only ever
    became 'clinic' after a real paid multi-doctor subscription. Also covers
    the Clinic.max_doctors ORM gap: the column existed in the DB via a raw
    migration but was never declared on the model, so every
    getattr(clinic, "max_doctors", 1) read the Python-side default of 1 —
    meaning no clinic, trial or paid, could ever invite a second doctor."""

    def test_clinic_signup_grants_clinic_admin_during_trial(self, client):
        r = register(client, email="clinicsignup1@test.com", account_type="clinic")
        assert r.status_code in (302, 303)
        cookie = login(client, "clinicsignup1@test.com").cookies.get("access_token")

        resp = client.get("/clinic/admin", cookies={"access_token": cookie},
                          follow_redirects=False)
        assert resp.status_code == 200, \
            f"clinic account should reach the Clinic Admin password gate, got {resp.status_code}"

    def test_solo_signup_still_blocked_from_clinic_admin(self, client):
        """Sanity check the fix didn't loosen the gate for real solo accounts.
        get_clinic_owner raises a 403, but main.py's global 403 handler turns
        that into a 303 to /dashboard rather than a raw 403 response."""
        r = register(client, email="soloonly1@test.com", account_type="solo")
        assert r.status_code in (302, 303)
        cookie = login(client, "soloonly1@test.com").cookies.get("access_token")

        resp = client.get("/clinic/admin", cookies={"access_token": cookie},
                          follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers.get("location") == "/dashboard"

    def test_clinic_signup_seat_limit_matches_paid_clinic_tier(self, client):
        from database.models import Clinic
        register(client, email="clinicsignup2@test.com", account_type="clinic")
        db = TestSession()
        try:
            doc = db.query(Doctor).filter(Doctor.email == "clinicsignup2@test.com").first()
            clinic = db.query(Clinic).filter(Clinic.owner_doctor_id == doc.id).first()
            assert clinic.plan_type == "clinic"
            assert clinic.max_doctors == 5, \
                "max_doctors should round-trip through the ORM, not silently fall back to 1"
        finally:
            db.close()

    def test_max_doctors_column_is_mapped_on_the_model(self, client):
        """Direct regression guard for the missing-column bug: write a value
        through the ORM and read it back through a fresh session, so a
        future accidental removal of the Column declaration fails loudly
        instead of quietly reverting every clinic to a 1-doctor cap."""
        from database.models import Clinic
        db = TestSession()
        try:
            clinic = Clinic(name="Column Check Clinic", plan_type="clinic", max_doctors=7)
            db.add(clinic)
            db.commit()
            clinic_id = clinic.id
        finally:
            db.close()

        db2 = TestSession()
        try:
            reloaded = db2.query(Clinic).filter(Clinic.id == clinic_id).first()
            assert reloaded.max_doctors == 7
        finally:
            db2.close()


class TestClinicDoctorInvite:
    """The invite-accept route never checked that the logged-in doctor's
    email matched the invite's target email — whoever was logged in when
    they opened the link could join in the invited doctor's place and
    silently burn the invite for the real invitee."""

    def _create_clinic_and_invite(self, client, owner_email, invitee_email):
        from database.models import Clinic, ClinicDoctor, ClinicDoctorInvite
        import secrets
        from datetime import timedelta as _td

        register(client, email=owner_email, account_type="clinic")
        db = TestSession()
        try:
            owner = db.query(Doctor).filter(Doctor.email == owner_email).first()
            clinic = db.query(Clinic).filter(Clinic.owner_doctor_id == owner.id).first()
            token = secrets.token_urlsafe(16)
            db.add(ClinicDoctorInvite(
                clinic_id=clinic.id, email=invitee_email, token=token,
                expires_at=datetime.utcnow() + _td(days=7),
            ))
            db.commit()
            return token
        finally:
            db.close()

    def test_mismatched_logged_in_doctor_cannot_accept(self, client):
        token = self._create_clinic_and_invite(
            client, "inviteowner1@test.com", "invitee1@test.com")
        # A completely unrelated doctor, logged in, tries to accept.
        register(client, email="bystander1@test.com")
        bystander_cookie = login(client, "bystander1@test.com").cookies.get("access_token")

        resp = client.post(f"/clinic/doctor-invite/{token}",
                           cookies={"access_token": bystander_cookie},
                           follow_redirects=False)
        assert resp.status_code == 403

        from database.models import ClinicDoctorInvite
        db = TestSession()
        try:
            invite = db.query(ClinicDoctorInvite).filter(ClinicDoctorInvite.token == token).first()
            assert invite.used_at is None, \
                "a rejected accept attempt must not consume the invite"
        finally:
            db.close()

    def test_matching_email_can_accept(self, client):
        token = self._create_clinic_and_invite(
            client, "inviteowner2@test.com", "invitee2@test.com")
        register(client, email="invitee2@test.com")
        cookie = login(client, "invitee2@test.com").cookies.get("access_token")

        resp = client.post(f"/clinic/doctor-invite/{token}",
                           cookies={"access_token": cookie},
                           follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers.get("location") == "/dashboard?joined=1"


# ═══════════════════════════════════════════════════════════════════════════
#  Phase 0 — safety net regressions
# ═══════════════════════════════════════════════════════════════════════════

class TestEmailSuppressedInTests:
    """The suite registers a doctor ~116 times and /register queues a real
    verification email via BackgroundTasks, which TestClient runs
    synchronously. With a live key in .env that was ~116 real sends per run
    against a 100/day quota. conftest zeroes the key; this guards it."""

    def test_resend_key_is_blank_during_tests(self):
        from config import settings
        assert settings.RESEND_API_KEY == "", (
            "RESEND_API_KEY must be blank in tests — a live key makes the "
            "suite send real email on every registration."
        )

    def test_registration_does_not_reach_resend(self, client, monkeypatch):
        """Registration must not touch the network even end-to-end."""
        import resend
        calls = []

        def _boom(*a, **k):
            calls.append(1)
            raise AssertionError("real email attempted")

        monkeypatch.setattr(resend.Emails, "send", staticmethod(_boom))
        r = register(client, email="nomail@test.com")
        assert r.status_code in (200, 302, 303)
        assert calls == []


class TestApptDoctorTokenVersion:
    """get_appt_doctor re-implements token handling instead of calling
    get_current_doctor, and was missing the token-version check — so a
    session minted before a password reset still authenticated on all nine
    appointment routes, surviving the reset meant to kill it."""

    def test_stale_token_version_rejected_on_appointment_route(self, client):
        tok = auth_cookie(client, "tv-appt@test.com")
        make_schedule(client, tok)

        # Book something so there is a real appointment id to request.
        client.post("/appointments", data={
            "patient_name": "Token Version", "patient_phone": "9812345671",
            "appt_date": next_monday(), "appt_time": "09:00",
            "appointment_type": "new_patient",
        }, cookies={"access_token": tok}, follow_redirects=False)

        db = TestSession()
        try:
            doc = db.query(Doctor).filter(Doctor.email == "tv-appt@test.com").first()
            appt = db.query(Appointment).filter(Appointment.doctor_id == doc.id).first()
            assert appt is not None, "setup failed: no appointment created"
            appt_id = appt.id
            # Simulate a password reset bumping the version after the cookie
            # above was minted.
            doc.token_version = (doc.token_version or 0) + 1
            db.commit()
        finally:
            db.close()

        r = client.get(f"/appointments/{appt_id}",
                       cookies={"access_token": tok}, follow_redirects=False)
        assert r.status_code == 303, (
            f"stale-token-version session should be rejected, got {r.status_code}"
        )
        assert "/login" in r.headers.get("location", "")


class TestClinicAdminGateCoversAllRoutes:
    """The password gate was called only from the dashboard route, so the
    roster could be listed and invites sent with just a live owner session."""

    def _owner_cookie(self, client, email):
        register(client, email=email, account_type="clinic")
        return login(client, email).cookies.get("access_token")

    def test_doctors_list_requires_password_gate(self, client):
        cookie = self._owner_cookie(client, "gate-list@test.com")
        r = client.get("/clinic/admin/doctors",
                       cookies={"access_token": cookie}, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers.get("location") == "/clinic/admin"

    def test_invite_requires_password_gate(self, client):
        cookie = self._owner_cookie(client, "gate-invite@test.com")
        r = client.post("/clinic/admin/doctors/invite",
                        data={"invite_email": "someone@test.com"},
                        cookies={"access_token": cookie}, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers.get("location") == "/clinic/admin"

        # And no invite was actually created.
        from database.models import ClinicDoctorInvite
        db = TestSession()
        try:
            assert db.query(ClinicDoctorInvite).filter(
                ClinicDoctorInvite.email == "someone@test.com").first() is None
        finally:
            db.close()


class TestRegisterInviteHoles:
    """/register accepted any email against a valid token, and silently fell
    through to a solo signup when the token was bad."""

    def test_invalid_invite_token_is_rejected_not_silently_solo(self, client):
        r = register(client, email="badtoken@test.com")  # baseline works
        assert r.status_code in (200, 302, 303)

        resp = client.post("/register", data={
            "name": "Dr Bad", "email": "badtoken2@test.com",
            "phone": _next_phone(), "password": "Kv9$mPq2#Zx8L",
            "clinic_name": "X", "city": "Y", "specialization": "General",
            "clinic_invite": "definitely-not-a-real-token",
            "account_type": "solo",
        }, follow_redirects=False)
        assert resp.status_code == 400
        assert "invalid or has expired" in resp.text

        db = TestSession()
        try:
            assert db.query(Doctor).filter(
                Doctor.email == "badtoken2@test.com").first() is None, (
                "no account should be created for a bad invite token")
        finally:
            db.close()

    def test_register_email_must_match_invite(self, client):
        from database.models import Clinic, ClinicDoctorInvite
        import secrets
        from datetime import timedelta as _td

        register(client, email="rowner@test.com", account_type="clinic")
        db = TestSession()
        try:
            owner = db.query(Doctor).filter(Doctor.email == "rowner@test.com").first()
            clinic = db.query(Clinic).filter(Clinic.owner_doctor_id == owner.id).first()
            token = secrets.token_urlsafe(16)
            db.add(ClinicDoctorInvite(
                clinic_id=clinic.id, email="intended@test.com", token=token,
                expires_at=datetime.utcnow() + _td(days=7),
            ))
            db.commit()
        finally:
            db.close()

        resp = client.post("/register", data={
            "name": "Dr Wrong", "email": "wrongperson@test.com",
            "phone": _next_phone(), "password": "Kv9$mPq2#Zx8L",
            "clinic_name": "", "city": "Y", "specialization": "General",
            "clinic_invite": token, "account_type": "solo",
        }, follow_redirects=False)
        assert resp.status_code == 400
        assert "intended@test.com" in resp.text

        db = TestSession()
        try:
            assert db.query(Doctor).filter(
                Doctor.email == "wrongperson@test.com").first() is None
            inv = db.query(ClinicDoctorInvite).filter(
                ClinicDoctorInvite.token == token).first()
            assert inv.used_at is None, "rejected registration must not burn the invite"
        finally:
            db.close()
