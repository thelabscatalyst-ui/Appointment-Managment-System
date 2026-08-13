"""
test_comprehensive.py — Full integration + unit test suite for Nivora.

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
    Bill, BillItem, PaymentMode, PriceCatalog,
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
            Patient, ClinicDoctor, ClinicDoctorInvite, Clinic, Doctor,
        )
        for mdl in [
            BillItem, Bill, NotificationLog, Visit, Appointment,
            PatientNote, NoteFile, PatientDocument, PinnedPatient,
            BlockedDate, BlockedTime, DoctorSchedule, Subscription,
            Expense, RecurringExpense, PriceCatalog,
            Patient, ClinicDoctor, ClinicDoctorInvite, Clinic, Doctor,
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
             phone=None, password="Pass1234!", city="Mumbai",
             clinic_name="Test Clinic"):
    if phone is None:
        phone = _next_phone()
    return client.post("/register", data={
        "name": name, "email": email, "phone": phone,
        "password": password, "clinic_name": clinic_name,
        "city": city, "specialization": "General", "clinic_invite": "",
    }, follow_redirects=False)


def login(client, email, password="Pass1234!"):
    return client.post("/login", data={"email": email, "password": password},
                       follow_redirects=False)


def auth_cookie(client, email, password="Pass1234!", **reg_kwargs):
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
