"""
test_public_booking.py — the pages strangers can reach without logging in.

Two booking surfaces: a doctor's personal link (/book/{slug}) and a clinic's
shared link (/book/clinic/{slug}) where the patient picks a doctor. Both are
unauthenticated and take free-text input from the open internet, so alongside
the happy paths these check that they cannot be used to enumerate patients,
book into a clinic the doctor does not belong to, or crash on junk.
"""
from datetime import date, datetime, timedelta

import pytest

from tests.conftest import TestSessionLocal
from tests.helpers import (make_doctor, clinic_of, make_patient, give_schedule,
                           phone, register, verify_email, login)
from database.models import (Doctor, Clinic, ClinicDoctor, Appointment, Patient,
                             AppointmentStatus)


def _next_weekday():
    """Tomorrow — avoids "past slots are hidden" on the current day."""
    return date.today() + timedelta(days=1)


@pytest.fixture
def pub(client):
    client.cookies.clear()
    email = f"pub-{datetime.utcnow().timestamp()}@test.com".replace(".", "-", 1)
    did = make_doctor(client, email, clinic_name="Public Test Clinic")
    cid = clinic_of(did)
    give_schedule(did, cid)
    db = TestSessionLocal()
    try:
        d = db.query(Doctor).filter(Doctor.id == did).first()
        c = db.query(Clinic).filter(Clinic.id == cid).first()
        c.plan_type = "clinic"
        c.max_doctors = 5
        c.plan_expires_at = datetime.utcnow() + timedelta(days=30)
        db.commit()
        out = {"id": did, "clinic": cid, "slug": d.slug, "clinic_slug": c.slug,
               "email": email}
    finally:
        db.close()
    client.cookies.clear()          # every test below is an anonymous visitor
    return out


def _book(client, slug, *, name="Public Booker", ph=None, on=None, at="10:00",
          kind="new_patient"):
    return client.post(f"/book/{slug}", data={
        "patient_name": name, "patient_phone": ph or phone(),
        "appt_date": (on or _next_weekday()).isoformat(), "appt_time": at,
        "appointment_type": kind, "patient_notes": "",
        "referral_source": "", "referral_source_other": "",
    }, follow_redirects=False)


# --------------------------------------------------------------------------- #
#  Doctor's personal booking link                                               #
# --------------------------------------------------------------------------- #

class TestDoctorBookingLink:

    def test_page_renders_without_login(self, client, pub):
        r = client.get(f"/book/{pub['slug']}")
        assert r.status_code == 200

    def test_unknown_slug_does_not_500(self, client, pub):
        assert client.get("/book/no-such-doctor").status_code in (200, 404)

    def test_slots_endpoint_returns_json(self, client, pub):
        r = client.get(f"/book/{pub['slug']}/slots?date={_next_weekday().isoformat()}")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")

    def test_slots_with_a_junk_date_does_not_500(self, client, pub):
        r = client.get(f"/book/{pub['slug']}/slots?date=not-a-date")
        assert r.status_code < 500

    def test_booking_creates_an_appointment_and_patient(self, client, pub):
        r = _book(client, pub["slug"], name="Anon Patient")
        assert r.status_code in (200, 302, 303), r.text[:300]

        db = TestSessionLocal()
        try:
            a = db.query(Appointment).filter(
                Appointment.doctor_id == pub["id"]).first()
            assert a is not None, "public booking created no appointment"
            assert a.clinic_id is not None, (
                "a public booking with clinic_id NULL is invisible in the app")
            p = db.query(Patient).filter(Patient.id == a.patient_id).first()
            assert p.name == "Anon Patient"
        finally:
            db.close()

    def test_confirmation_page_renders(self, client, pub):
        _book(client, pub["slug"], name="Confirm Me")
        db = TestSessionLocal()
        try:
            aid = db.query(Appointment).filter(
                Appointment.doctor_id == pub["id"]).first().id
        finally:
            db.close()
        assert client.get(f"/book/{pub['slug']}/confirm/{aid}").status_code == 200

    def test_confirmation_of_a_foreign_appointment_is_refused(self, client, pub):
        """The id is a small integer — it must not be a lookup oracle."""
        other = make_doctor(client, "pub-other-doc@test.com")
        other_clinic = clinic_of(other)
        op = make_patient(other, other_clinic, name="Someone Elses Patient")
        db = TestSessionLocal()
        try:
            a = Appointment(doctor_id=other, patient_id=op, clinic_id=other_clinic,
                            appointment_date=_next_weekday(),
                            appointment_time=datetime.now().time().replace(
                                minute=0, second=0, microsecond=0),
                            duration_mins=15, status=AppointmentStatus.scheduled)
            db.add(a); db.commit(); db.refresh(a)
            foreign = a.id
        finally:
            db.close()

        client.cookies.clear()
        r = client.get(f"/book/{pub['slug']}/confirm/{foreign}", follow_redirects=False)
        assert r.status_code != 200 or "Someone Elses Patient" not in r.text, (
            "one doctor's booking link confirmed another doctor's appointment")

    def test_short_phone_is_rejected(self, client, pub):
        r = _book(client, pub["slug"], ph="123")
        assert r.status_code < 500
        db = TestSessionLocal()
        try:
            assert db.query(Patient).filter(Patient.phone == "123").count() == 0
        finally:
            db.close()

    def test_junk_date_is_rejected_without_crashing(self, client, pub):
        r = client.post(f"/book/{pub['slug']}", data={
            "patient_name": "Bad Date", "patient_phone": phone(),
            "appt_date": "31-31-9999", "appt_time": "10:00",
            "appointment_type": "new_patient", "patient_notes": "",
            "referral_source": "", "referral_source_other": "",
        }, follow_redirects=False)
        assert r.status_code < 500

    def test_unknown_appointment_type_does_not_500(self, client, pub):
        assert _book(client, pub["slug"], kind="not-a-type").status_code < 500

    def test_booking_page_does_not_list_existing_patients(self, client, pub):
        """A public page must not become a patient directory."""
        make_patient(pub["id"], pub["clinic"], name="Private Existing Patient")
        client.cookies.clear()
        body = client.get(f"/book/{pub['slug']}").text
        assert "Private Existing Patient" not in body


# --------------------------------------------------------------------------- #
#  Clinic booking link                                                          #
# --------------------------------------------------------------------------- #

class TestClinicBookingLink:

    def test_page_renders_and_lists_the_clinic(self, client, pub):
        r = client.get(f"/book/clinic/{pub['clinic_slug']}")
        assert r.status_code == 200
        assert "Public Test Clinic" in r.text

    def test_unknown_clinic_slug_does_not_500(self, client, pub):
        assert client.get("/book/clinic/no-such-clinic").status_code in (200, 404)

    def test_slots_endpoint_returns_json(self, client, pub):
        r = client.get(f"/book/clinic/{pub['clinic_slug']}/slots"
                       f"?date={_next_weekday().isoformat()}&doctor_id={pub['id']}")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")

    def test_booking_through_the_clinic_link(self, client, pub):
        r = client.post(f"/book/clinic/{pub['clinic_slug']}", data={
            "doctor_id": pub["id"], "patient_name": "Clinic Booker",
            "patient_phone": phone(),
            "appt_date": _next_weekday().isoformat(), "appt_time": "10:00",
            "appointment_type": "new_patient", "patient_notes": "",
            "referral_source": "", "referral_source_other": "",
        }, follow_redirects=False)
        assert r.status_code in (200, 302, 303), r.text[:300]

        db = TestSessionLocal()
        try:
            a = db.query(Appointment).filter(
                Appointment.doctor_id == pub["id"]).first()
            assert a is not None
            assert a.clinic_id == pub["clinic"], (
                "a clinic booking was not attributed to that clinic")
        finally:
            db.close()

    def test_cannot_book_a_doctor_who_is_not_in_the_clinic(self, client, pub):
        """doctor_id is posted by the browser — membership is the authority."""
        outsider = make_doctor(client, "pub-outsider@test.com")
        client.cookies.clear()

        client.post(f"/book/clinic/{pub['clinic_slug']}", data={
            "doctor_id": outsider, "patient_name": "Wrong Doctor",
            "patient_phone": phone(),
            "appt_date": _next_weekday().isoformat(), "appt_time": "10:00",
            "appointment_type": "new_patient", "patient_notes": "",
            "referral_source": "", "referral_source_other": "",
        }, follow_redirects=False)

        db = TestSessionLocal()
        try:
            leaked = db.query(Appointment).filter(
                Appointment.doctor_id == outsider,
                Appointment.clinic_id == pub["clinic"]).count()
            assert leaked == 0, (
                "a stranger booked a doctor into a clinic they do not belong to")
        finally:
            db.close()

    def test_clinic_page_does_not_leak_patient_names(self, client, pub):
        make_patient(pub["id"], pub["clinic"], name="Clinic Private Patient")
        client.cookies.clear()
        body = client.get(f"/book/clinic/{pub['clinic_slug']}").text
        assert "Clinic Private Patient" not in body


# --------------------------------------------------------------------------- #
#  Static public endpoints                                                      #
# --------------------------------------------------------------------------- #

class TestStaticPublicEndpoints:

    def test_landing_page(self, client):
        client.cookies.clear()
        assert client.get("/", follow_redirects=False).status_code in (200, 303)

    def test_pricing_page(self, client):
        client.cookies.clear()
        assert client.get("/pricing").status_code == 200

    def test_robots_and_sitemap(self, client):
        client.cookies.clear()
        r = client.get("/robots.txt")
        assert r.status_code == 200 and "Disallow" in r.text
        assert client.get("/sitemap.xml").status_code == 200

    def test_robots_and_sitemap_point_at_a_live_host(self, client):
        """They advertised medtrack.life, which has no DNS record at all."""
        client.cookies.clear()
        body = client.get("/robots.txt").text + client.get("/sitemap.xml").text
        assert "medtrack.life" not in body, (
            "robots/sitemap still advertise a domain that does not resolve")
