"""
conftest.py — shared fixtures for Nivora test suite.
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

from database.connection import Base, get_db

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
    """Create all tables once for the session."""
    from database import models  # noqa — registers models with Base
    Base.metadata.create_all(bind=test_engine)
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
        from database.models import (
            BillItem, Bill, NotificationLog, Visit,
            Appointment, PatientNote, NoteFile, PatientDocument,
            PinnedPatient, BlockedDate, BlockedTime, DoctorSchedule,
            Subscription, Expense, RecurringExpense, PriceCatalog,
            Patient, ClinicDoctor, ClinicDoctorInvite, EmailVerification, Clinic, Doctor,
        )
        for model in [
            BillItem, Bill, NotificationLog, Visit,
            Appointment, PatientNote, NoteFile, PatientDocument,
            PinnedPatient, BlockedDate, BlockedTime, DoctorSchedule,
            Subscription, Expense, RecurringExpense, PriceCatalog,
            Patient, ClinicDoctor, ClinicDoctorInvite, EmailVerification, Clinic, Doctor,
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

def register_doctor(client, *, name, email, phone, password="Wq7$mzKp9Xv2Ld", city="TestCity", clinic_name="Test Clinic"):
    """Register a doctor and return the response."""
    return client.post("/register", data={
        "name": name,
        "email": email,
        "phone": phone,
        "password": password,
        "clinic_name": clinic_name,
        "city": city,
        "specialization": "General",
        "clinic_invite": "",
    }, follow_redirects=False)


def login_doctor(client, email, password="Wq7$mzKp9Xv2Ld"):
    """Login and return the response (has Set-Cookie if successful)."""
    return client.post("/login", data={
        "email": email,
        "password": password,
    }, follow_redirects=False)


def get_auth_client(client, email, password="Wq7$mzKp9Xv2Ld"):
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
