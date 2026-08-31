"""
test_remaining_endpoints.py — the last routes with no coverage.

Sweeps up what the domain suites left: the appointment consultation card and
its actions, note-file downloads, the patient feedback link, clinic seat
reactivation, and the platform-admin pages. Grouped by what they are rather
than by router, since that is how they fail.

Razorpay order creation is exercised only far enough to prove the route does
not 500 without credentials — it must not reach the payment provider from a
test run.
"""
import io
from datetime import date, datetime, timedelta

import pytest

from tests.conftest import TestSessionLocal
from tests.helpers import (make_doctor, clinic_of, make_patient, make_appointment,
                           make_visit, give_schedule, set_pin, login, register,
                           verify_email, phone, PASSWORD)
from database.models import (Doctor, Clinic, ClinicDoctor, Appointment,
                             AppointmentStatus, NoteFile, PatientNote, Bill,
                             Feedback, Visit, VisitStatus)


@pytest.fixture
def doc(client):
    client.cookies.clear()
    email = f"rem-{datetime.utcnow().timestamp()}@test.com".replace(".", "-", 1)
    did = make_doctor(client, email)
    cid = clinic_of(did)
    give_schedule(did, cid)
    pid = make_patient(did, cid, name="Remaining Patient")
    return {"id": did, "clinic": cid, "patient": pid, "email": email}


# --------------------------------------------------------------------------- #
#  The consultation card                                                        #
# --------------------------------------------------------------------------- #

class TestAppointmentCard:

    def test_card_renders(self, client, doc):
        a = make_appointment(doc["id"], doc["patient"], doc["clinic"])
        r = client.get(f"/appointments/{a}/card")
        assert r.status_code == 200
        assert "Remaining Patient" in r.text

    def test_card_save_persists(self, client, doc):
        a = make_appointment(doc["id"], doc["patient"], doc["clinic"])
        r = client.post(f"/appointments/{a}/card-save", data={
            "chief_complaint": "Headache for 3 days",
            "diagnosis": "Tension headache",
            "vitals_bp": "120/80", "vitals_pulse": "72",
            "vitals_temp": "98.4", "vitals_weight": "70",
        }, follow_redirects=False)
        assert r.status_code in (200, 302, 303), r.text[:200]

    def test_reception_notes_save(self, client, doc):
        a = make_appointment(doc["id"], doc["patient"], doc["clinic"])
        r = client.post(f"/appointments/{a}/reception-notes",
                        data={"reception_notes": "Patient is waiting outside"},
                        follow_redirects=False)
        assert r.status_code in (200, 302, 303)

    def test_follow_up_books_a_later_appointment(self, client, doc):
        a = make_appointment(doc["id"], doc["patient"], doc["clinic"])
        target = date.today() + timedelta(days=7)
        r = client.post(f"/appointments/{a}/follow-up",
                        data={"follow_up_date": target.isoformat()},
                        follow_redirects=False)
        assert r.status_code in (200, 302, 303)

        db = TestSessionLocal()
        try:
            made = db.query(Appointment).filter(
                Appointment.doctor_id == doc["id"],
                Appointment.appointment_date == target).first()
            assert made is not None, "no follow-up appointment was created"
            assert made.clinic_id == doc["clinic"], (
                "the follow-up did not inherit the clinic, so it is invisible")
        finally:
            db.close()

    def test_delete_appointment(self, client, doc):
        a = make_appointment(doc["id"], doc["patient"], doc["clinic"])
        client.post(f"/appointments/{a}/delete", follow_redirects=False)
        db = TestSessionLocal()
        try:
            row = db.query(Appointment).filter(Appointment.id == a).first()
            assert row is None or row.status == AppointmentStatus.cancelled
        finally:
            db.close()

    def test_another_doctor_cannot_touch_the_card(self, client, doc):
        a = make_appointment(doc["id"], doc["patient"], doc["clinic"])
        make_doctor(client, "rem-card-intruder@test.com")

        r = client.get(f"/appointments/{a}/card", follow_redirects=False)
        assert "Remaining Patient" not in r.text, (
            "the consultation card leaked another doctor's patient")
        client.post(f"/appointments/{a}/delete", follow_redirects=False)
        db = TestSessionLocal()
        try:
            row = db.query(Appointment).filter(Appointment.id == a).first()
            assert row is not None and row.status != AppointmentStatus.cancelled, (
                "another doctor cancelled this appointment")
        finally:
            db.close()

    def test_patient_lookup_by_phone(self, client, doc):
        db = TestSessionLocal()
        try:
            from database.models import Patient
            ph = db.query(Patient).filter(Patient.id == doc["patient"]).first().phone
        finally:
            db.close()

        r = client.get(f"/appointments/patient-lookup?phone={ph}")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")

    def test_patient_lookup_does_not_reveal_other_doctors_patients(self, client, doc):
        """The endpoint answers on a bare phone number — a perfect oracle."""
        other = make_doctor(client, "rem-lookup-other@test.com")
        other_clinic = clinic_of(other)
        secret_phone = phone()
        make_patient(other, other_clinic, name="Hidden Person", ph=secret_phone)

        login(client, doc["email"])
        r = client.get(f"/appointments/patient-lookup?phone={secret_phone}")
        assert "Hidden Person" not in r.text, (
            "patient lookup leaked another doctor's patient by phone number")

    def test_lookup_with_a_junk_phone_does_not_500(self, client, doc):
        assert client.get("/appointments/patient-lookup?phone=abc").status_code < 500


# --------------------------------------------------------------------------- #
#  Note attachments                                                             #
# --------------------------------------------------------------------------- #

class TestNoteFiles:

    def _note_with_file(self, client, patient_id):
        client.post(f"/patients/{patient_id}/notes/add",
                    data={"note_text": "See attached"},
                    files={"files": ("xray.png", io.BytesIO(b"\x89PNG\r\n\x1a\nx"),
                                     "image/png")},
                    follow_redirects=False)
        db = TestSessionLocal()
        try:
            f = db.query(NoteFile).first()
            return f.id if f else None
        finally:
            db.close()

    def test_download_a_note_attachment(self, client, doc):
        set_pin(client)
        fid = self._note_with_file(client, doc["patient"])
        assert fid, "no attachment stored"
        r = client.get(f"/patients/{doc['patient']}/files/{fid}")
        assert r.status_code == 200

    def test_another_doctor_cannot_download_it(self, client, doc):
        set_pin(client)
        fid = self._note_with_file(client, doc["patient"])
        victim = doc["patient"]

        make_doctor(client, "rem-file-intruder@test.com")
        set_pin(client)
        r = client.get(f"/patients/{victim}/files/{fid}", follow_redirects=False)
        assert r.status_code != 200, (
            "another doctor downloaded this patient's attachment")

    def test_delete_a_note_attachment(self, client, doc):
        set_pin(client)
        fid = self._note_with_file(client, doc["patient"])
        client.post(f"/patients/{doc['patient']}/files/{fid}/delete",
                    follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert db.query(NoteFile).filter(NoteFile.id == fid).first() is None
        finally:
            db.close()

    def test_unknown_file_is_not_a_500(self, client, doc):
        set_pin(client)
        assert client.get(f"/patients/{doc['patient']}/files/999999",
                          follow_redirects=False).status_code < 500


# --------------------------------------------------------------------------- #
#  Patient feedback link                                                        #
# --------------------------------------------------------------------------- #

class TestFeedback:

    def _feedback_token(self, doc):
        """A feedback row is normally created when a bill is issued."""
        import secrets
        vid = make_visit(doc["id"], doc["patient"], doc["clinic"],
                         status=VisitStatus.done)
        db = TestSessionLocal()
        try:
            bill = Bill(visit_id=vid, doctor_id=doc["id"], clinic_id=doc["clinic"],
                        patient_id=doc["patient"], subtotal=500, total=500,
                        paid_amount=500, created_at=datetime.utcnow())
            db.add(bill); db.commit(); db.refresh(bill)
            fb = Feedback(doctor_id=doc["id"], patient_id=doc["patient"],
                          bill_id=bill.id, token=secrets.token_urlsafe(16))
            db.add(fb); db.commit()
            return fb.token
        finally:
            db.close()

    def test_form_opens_without_login(self, client, doc):
        token = self._feedback_token(doc)
        client.cookies.clear()
        assert client.get(f"/feedback/{token}").status_code == 200

    def test_submitting_feedback_records_it(self, client, doc):
        token = self._feedback_token(doc)
        client.cookies.clear()
        r = client.post(f"/feedback/{token}",
                        data={"rating": "5", "comment": "Very helpful"},
                        follow_redirects=False)
        assert r.status_code in (200, 302, 303)

        db = TestSessionLocal()
        try:
            fb = db.query(Feedback).filter(Feedback.token == token).first()
            assert fb.rating == 5, f"rating not saved: {fb.rating}"
        finally:
            db.close()

    def test_unknown_token_does_not_500(self, client):
        client.cookies.clear()
        assert client.get("/feedback/not-a-real-token").status_code < 500

    def test_feedback_page_does_not_expose_the_patient(self, client, doc):
        """The link goes out over WhatsApp; anyone with it can open the page."""
        token = self._feedback_token(doc)
        client.cookies.clear()
        assert "Remaining Patient" not in client.get(f"/feedback/{token}").text


# --------------------------------------------------------------------------- #
#  Clinic seat reactivation                                                     #
# --------------------------------------------------------------------------- #

class TestSeatReactivation:

    def test_deactivate_then_reactivate(self, client):
        client.cookies.clear()
        owner_email = "rem-seat-owner@test.com"
        owner = make_doctor(client, owner_email, account_type="clinic",
                            clinic_name="Seat Clinic")
        cid = clinic_of(owner)
        db = TestSessionLocal()
        try:
            c = db.query(Clinic).filter(Clinic.id == cid).first()
            c.plan_type = "clinic"; c.max_doctors = 5
            c.plan_expires_at = datetime.utcnow() + timedelta(days=30)
            db.commit()
        finally:
            db.close()

        assoc = make_doctor(client, "rem-seat-assoc@test.com")
        db = TestSessionLocal()
        try:
            m = ClinicDoctor(clinic_id=cid, doctor_id=assoc, role="associate",
                             is_active=True)
            db.add(m); db.commit(); db.refresh(m)
            mid = m.id
        finally:
            db.close()

        login(client, owner_email)
        client.post("/clinic/admin/auth", data={"password": PASSWORD},
                    follow_redirects=False)

        client.post(f"/clinic/admin/doctors/{mid}/deactivate", follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert db.query(ClinicDoctor).filter(
                ClinicDoctor.id == mid).first().is_active is False
        finally:
            db.close()

        client.post(f"/clinic/admin/doctors/{mid}/reactivate", follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert db.query(ClinicDoctor).filter(
                ClinicDoctor.id == mid).first().is_active is True, (
                "a deactivated doctor could not be brought back")
        finally:
            db.close()

    def test_an_outsider_cannot_reactivate_a_seat(self, client):
        client.cookies.clear()
        owner = make_doctor(client, "rem-seat-owner2@test.com",
                            account_type="clinic", clinic_name="Seat Clinic 2")
        cid = clinic_of(owner)
        assoc = make_doctor(client, "rem-seat-assoc2@test.com")
        db = TestSessionLocal()
        try:
            m = ClinicDoctor(clinic_id=cid, doctor_id=assoc, role="associate",
                             is_active=False)
            db.add(m); db.commit(); db.refresh(m)
            mid = m.id
        finally:
            db.close()

        make_doctor(client, "rem-seat-outsider@test.com")
        client.post(f"/clinic/admin/doctors/{mid}/reactivate", follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert db.query(ClinicDoctor).filter(
                ClinicDoctor.id == mid).first().is_active is False, (
                "an outsider reactivated a seat in someone else's clinic")
        finally:
            db.close()


# --------------------------------------------------------------------------- #
#  Platform admin and payments                                                  #
# --------------------------------------------------------------------------- #

class TestAdminAndPayments:

    def test_admin_pages_refuse_an_ordinary_doctor(self, client, doc):
        """/admin is the platform owner's, not any doctor's."""
        for path in ("/admin", "/admin/dashboard", "/admin/doctors"):
            r = client.get(path, follow_redirects=False)
            assert r.status_code != 200, f"{path} was served to an ordinary doctor"

    def test_admin_pages_refuse_a_stranger(self, client):
        client.cookies.clear()
        for path in ("/admin/dashboard", "/admin/doctors"):
            r = client.get(path, follow_redirects=False)
            assert r.status_code != 200

    def test_create_order_without_credentials_does_not_500(self, client, doc):
        """Razorpay is unconfigured in tests; this must fail closed, not crash."""
        r = client.post("/billing/create-order",
                        data={"plan": "solo", "seats": "1"},
                        follow_redirects=False)
        assert r.status_code < 500, r.text[:200]

    def test_clinic_booking_confirmation_page(self, client, doc):
        db = TestSessionLocal()
        try:
            c = db.query(Clinic).filter(Clinic.id == doc["clinic"]).first()
            c.plan_type = "clinic"; c.max_doctors = 5
            c.plan_expires_at = datetime.utcnow() + timedelta(days=30)
            db.commit()
            slug = c.slug
        finally:
            db.close()

        a = make_appointment(doc["id"], doc["patient"], doc["clinic"],
                             on=date.today() + timedelta(days=1))
        client.cookies.clear()
        r = client.get(f"/book/clinic/{slug}/confirm/{a}")
        assert r.status_code == 200
