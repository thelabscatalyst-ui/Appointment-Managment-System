"""
conftest.py — shared fixtures for Med Track test suite.
"""
import os
import sys
from datetime import datetime, timedelta, date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Ensure the project root is on sys.path so imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── in-memory SQLite ────────────────────────────────────────────────────────
TEST_DATABASE_URL = "sqlite:///./test_clinic.db"
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

# ── Email: hard-off for the entire suite ────────────────────────────────────
# The suite registers a doctor ~116 times, and POST /register queues a real
# verification email through BackgroundTasks — which TestClient executes
# synchronously as part of the response. config.py loads .env for tests too,
# so a live RESEND_API_KEY meant ~116 real sends per run against a 100/day
# quota.
#
# Zeroing the key makes services.email_service.send_email short-circuit
# before any network I/O and return ("not configured"). Deliberately NOT
# monkeypatching send_email itself: TestEmailService/TestInviteService call
# it directly and assert on that exact return value.
#
# This import must stay BELOW the DATABASE_URL line above — config builds its
# Settings singleton at import time, so importing it any earlier would bind
# the suite to the real dev database.
from config import settings as _test_settings           # noqa: E402
_test_settings.RESEND_API_KEY = ""

from database.connection import Base, get_db            # noqa: E402

test_engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
)
TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


def override_get_db():
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(scope="session", autouse=True)
def setup_test_database():
    """Create all tables once for the session.

    Goes through create_tables() rather than Base.metadata.create_all() so the
    additive migrations in _run_migrations() are exercised by the suite —
    otherwise every migration ships untested. Safe because conftest sets
    DATABASE_URL above before database.connection is imported, so that
    module's engine is this same test database.
    """
    from database import models  # noqa — registers models with Base
    from database.connection import create_tables
    create_tables()
    yield
    Base.metadata.drop_all(bind=test_engine)
    # Clean up the test db file
    try:
        os.remove("test_clinic.db")
    except FileNotFoundError:
        pass


@pytest.fixture(autouse=True)
def clean_tables():
    """Truncate all tables before each test for isolation."""
    db = TestSessionLocal()
    try:
        # Delete in dependency order to avoid FK violations
        # Feedback, Prescription and PrescriptionItem were missing from this
        # list, so those rows survived into the next test. Any assertion that
        # counted prescriptions saw every earlier test's leftovers, which both
        # produces flaky failures and hides real ones.
        from database.models import (
            Feedback, PrescriptionItem, Prescription,
            BillItem, Bill, NotificationLog, Visit,
            Appointment, PatientNote, NoteFile, PatientDocument,
            PinnedPatient, BlockedDate, BlockedTime, DoctorSchedule,
            Subscription, Expense, RecurringExpense, PriceCatalog,
            Patient, ClinicDoctor, ClinicDoctorInvite, EmailVerification, PasswordReset, Clinic, Doctor,
        )
        # Children before parents — SQLite enforces no FKs here, but Postgres
        # would, and this list is the closest thing to a schema teardown order.
        for model in [
            Feedback, PrescriptionItem, Prescription,
            BillItem, Bill, NotificationLog, Visit,
            Appointment, PatientNote, NoteFile, PatientDocument,
            PinnedPatient, BlockedDate, BlockedTime, DoctorSchedule,
            Subscription, Expense, RecurringExpense, PriceCatalog,
            Patient, ClinicDoctor, ClinicDoctorInvite, EmailVerification, PasswordReset, Clinic, Doctor,
        ]:
            db.query(model).delete()
        db.commit()
    finally:
        db.close()
    yield


@pytest.fixture(scope="session")
def client():
    """FastAPI TestClient with test DB override, scheduler disabled."""
    from unittest.mock import patch
    # Patch scheduler so it doesn't start/stop background jobs
    with patch("services.scheduler_service.start_scheduler"), \
         patch("services.scheduler_service.stop_scheduler"):
        from main import app
        app.dependency_overrides[get_db] = override_get_db
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


# ── helpers ─────────────────────────────────────────────────────────────────

def register_doctor(client, *, name, email, phone, password="Kv9$mPq2#Zx8L", city="TestCity", clinic_name="Test Clinic"):
    """Register a doctor and return the response.

    Auto-verifies the email in the DB afterward. Email verification is now
    mandatory (get_paying_doctor raises EmailNotVerified until it's set), and
    it has its own dedicated coverage — other tests that just need a working
    logged-in doctor shouldn't have to route around that gate.
    """
    resp = client.post("/register", data={
        "name": name,
        "email": email,
        "phone": phone,
        "password": password,
        "clinic_name": clinic_name,
        "city": city,
        "specialization": "General",
        "clinic_invite": "",
    }, follow_redirects=False)
    if resp.status_code in (200, 302, 303):
        from database.models import Doctor
        db = TestSessionLocal()
        try:
            doc = db.query(Doctor).filter(Doctor.email == email.strip().lower()).first()
            if doc and not doc.email_verified_at:
                doc.email_verified_at = datetime.utcnow()
                db.commit()
        finally:
            db.close()
    return resp


def login_doctor(client, email, password="Kv9$mPq2#Zx8L"):
    """Login and return the response (has Set-Cookie if successful)."""
    return client.post("/login", data={
        "email": email,
        "password": password,
    }, follow_redirects=False)


def get_auth_client(client, email, password="Kv9$mPq2#Zx8L"):
    """Login and return (client, cookie_dict) with auth cookie set."""
    resp = login_doctor(client, email, password)
    assert resp.status_code == 303, f"Login failed for {email}: {resp.status_code}"
    cookie = resp.cookies.get("access_token")
    assert cookie, "No access_token cookie set"
    return cookie


def make_schedule(client, cookie, day_of_week=0):
    """Set a working schedule for the logged-in doctor: Mon 09:00–17:00, 15-min slots."""
    data = {
        f"active_{day_of_week}": "on",
        f"shift_start_{day_of_week}_0": "09:00",
        f"shift_end_{day_of_week}_0": "17:00",
        f"slot_{day_of_week}": "15",
        f"max_{day_of_week}": "30",
        f"walkin_buf_{day_of_week}": "0",
        "avg_consult_mins": "10",
    }
    return client.post(
        "/doctors/settings/schedule",
        data=data,
        cookies={"access_token": cookie},
        follow_redirects=False,
    )
