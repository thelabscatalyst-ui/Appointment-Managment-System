"""
test_visits_queue.py — the live consultation queue.

This is the screen a clinic actually runs the day from, and it was the largest
untested surface in the app: check-in, call, hold, resume, skip, cancel,
emergency promotion, manual reordering, and the public waiting-room display.

Every state-changing route is checked twice — that it does the thing, and that
another doctor cannot do it to your queue.
"""
from datetime import date, datetime, timedelta

import pytest

from tests.conftest import TestSessionLocal
from tests.helpers import (make_doctor, clinic_of, make_patient, make_appointment,
                           make_visit, visit_row, appt_row, give_schedule,
                           phone, count, register, verify_email, login)
from database.models import Visit, VisitStatus, Patient, AppointmentStatus, Doctor


@pytest.fixture
def doc(client):
    """A logged-in doctor with a clinic, open hours, and one patient."""
    client.cookies.clear()
    email = f"queue-{datetime.utcnow().timestamp()}@test.com".replace(".", "-", 1)
    did = make_doctor(client, email)
    cid = clinic_of(did)
    give_schedule(did, cid)
    pid = make_patient(did, cid, name="Queue Patient")
    return {"id": did, "clinic": cid, "patient": pid, "email": email}


# --------------------------------------------------------------------------- #
#  Check-in                                                                     #
# --------------------------------------------------------------------------- #

class TestCheckIn:

    def test_walk_in_check_in_creates_a_waiting_visit(self, client, doc):
        r = client.post("/visits/check-in", data={
            "name": "Walk In Person", "phone": phone(), "notes": "cough",
        }, follow_redirects=False)
        assert r.status_code in (200, 302, 303), r.text[:300]

        db = TestSessionLocal()
        try:
            v = db.query(Visit).filter(Visit.doctor_id == doc["id"]).first()
            assert v is not None, "no visit created"
            assert v.status == VisitStatus.waiting
            assert v.visit_date == date.today()
            assert v.token_number and v.token_number >= 1
        finally:
            db.close()

    def test_check_in_stamps_the_active_clinic(self, client, doc):
        """A visit with clinic_id NULL is invisible to every clinic-scoped read."""
        client.post("/visits/check-in", data={"name": "Stamped", "phone": phone()},
                    follow_redirects=False)
        db = TestSessionLocal()
        try:
            v = db.query(Visit).filter(Visit.doctor_id == doc["id"]).first()
            assert v.clinic_id == doc["clinic"], (
                f"visit stamped {v.clinic_id}, expected {doc['clinic']}")
        finally:
            db.close()

    def test_check_in_reuses_an_existing_patient_by_phone(self, client, doc):
        """Same person, same day, twice — not two patient records."""
        ph = phone()
        client.post("/visits/check-in", data={"name": "Repeat Person", "phone": ph},
                    follow_redirects=False)
        before = count(Patient, doctor_id=doc["id"])
        client.post("/visits/check-in", data={"name": "Repeat Person", "phone": ph},
                    follow_redirects=False)
        assert count(Patient, doctor_id=doc["id"]) == before, (
            "checking the same phone in twice created a duplicate patient")

    def test_emergency_check_in_is_flagged(self, client, doc):
        client.post("/visits/check-in", data={
            "name": "Urgent Person", "phone": phone(), "is_emergency": "true",
        }, follow_redirects=False)
        db = TestSessionLocal()
        try:
            v = (db.query(Visit).join(Patient)
                   .filter(Patient.name == "Urgent Person").first())
            assert v is not None and v.is_emergency is True
        finally:
            db.close()

    def test_check_in_from_an_appointment(self, client, doc):
        appt = make_appointment(doc["id"], doc["patient"], doc["clinic"])
        r = client.post(f"/visits/check-in-appt/{appt}", follow_redirects=False)
        assert r.status_code in (200, 302, 303)

        db = TestSessionLocal()
        try:
            v = db.query(Visit).filter(Visit.appointment_id == appt).first()
            assert v is not None, "appointment check-in created no visit"
            assert v.patient_id == doc["patient"]
        finally:
            db.close()

    def test_cannot_check_in_another_doctors_appointment(self, client, doc):
        other = make_doctor(client, "queue-thief-appt@test.com")
        appt = make_appointment(doc["id"], doc["patient"], doc["clinic"])
        # `client` is now the other doctor.
        client.post(f"/visits/check-in-appt/{appt}", follow_redirects=False)
        db = TestSessionLocal()
        try:
            v = db.query(Visit).filter(Visit.appointment_id == appt).first()
            assert v is None or v.doctor_id != other, (
                "a doctor checked in someone else's appointment")
        finally:
            db.close()


# --------------------------------------------------------------------------- #
#  Queue state machine                                                          #
# --------------------------------------------------------------------------- #

class TestQueueTransitions:

    def test_call_moves_waiting_to_serving(self, client, doc):
        v = make_visit(doc["id"], doc["patient"], doc["clinic"])
        r = client.post(f"/visits/{v}/call", follow_redirects=False)
        assert r.status_code in (200, 302, 303)
        row = visit_row(v)
        assert row["status"] == VisitStatus.serving
        assert row["call_time"] is not None, "call_time drives the wait-time report"

    def test_done_ends_the_consultation(self, client, doc):
        v = make_visit(doc["id"], doc["patient"], doc["clinic"],
                       status=VisitStatus.serving)
        client.post(f"/visits/{v}/done", follow_redirects=False)
        assert visit_row(v)["status"] in (
            VisitStatus.billing_pending, VisitStatus.done), visit_row(v)

    def test_hold_then_resume_round_trips(self, client, doc):
        """Sent for an X-ray and back — the patient keeps their place."""
        v = make_visit(doc["id"], doc["patient"], doc["clinic"],
                       status=VisitStatus.serving)
        client.post(f"/visits/{v}/hold", follow_redirects=False)
        assert visit_row(v)["status"] == VisitStatus.on_hold

        client.post(f"/visits/{v}/resume", follow_redirects=False)
        assert visit_row(v)["status"] in (VisitStatus.waiting, VisitStatus.serving)

    def test_skip_defers_without_losing_the_visit(self, client, doc):
        v = make_visit(doc["id"], doc["patient"], doc["clinic"])
        client.post(f"/visits/{v}/skip", follow_redirects=False)
        row = visit_row(v)
        assert row is not None, "skip deleted the visit"
        assert row["status"] != VisitStatus.serving

    def test_cancel_removes_from_the_queue(self, client, doc):
        v = make_visit(doc["id"], doc["patient"], doc["clinic"])
        client.post(f"/visits/{v}/cancel", follow_redirects=False)
        row = visit_row(v)
        assert row is None or row["status"] == VisitStatus.cancelled

    def test_emergency_promotes_to_the_front(self, client, doc):
        first = make_visit(doc["id"], doc["patient"], doc["clinic"], position=1, token=1)
        p2 = make_patient(doc["id"], doc["clinic"], name="Second In Line")
        last = make_visit(doc["id"], p2, doc["clinic"], position=2, token=2)

        client.post(f"/visits/{last}/emergency", follow_redirects=False)
        assert visit_row(last)["emergency"] is True
        assert visit_row(last)["position"] <= visit_row(first)["position"], (
            "an emergency was not moved ahead of the waiting patient")

    def test_emergency_does_not_renumber_another_clinics_queue(self, client, doc):
        """Promotion once renumbered every queue the doctor had, anywhere."""
        other_doc = make_doctor(client, "queue-other-clinic@test.com")
        other_clinic = clinic_of(other_doc)
        other_p = make_patient(other_doc, other_clinic, name="Untouched")
        other_v = make_visit(other_doc, other_p, other_clinic, position=7, token=7)
        before = visit_row(other_v)["position"]

        login(client, doc["email"])
        v = make_visit(doc["id"], doc["patient"], doc["clinic"], position=1, token=1)
        client.post(f"/visits/{v}/emergency", follow_redirects=False)

        assert visit_row(other_v)["position"] == before, (
            "promoting an emergency renumbered a different clinic's queue")

    def test_move_reorders_within_the_queue(self, client, doc):
        a = make_visit(doc["id"], doc["patient"], doc["clinic"], position=1, token=1)
        p2 = make_patient(doc["id"], doc["clinic"], name="Mover Two")
        b = make_visit(doc["id"], p2, doc["clinic"], position=2, token=2)

        r = client.post(f"/visits/{b}/move", data={"new_position": 1},
                        follow_redirects=False)
        assert r.status_code in (200, 302, 303)
        assert visit_row(b)["position"] <= visit_row(a)["position"]

    def test_close_free_settles_without_a_bill(self, client, doc):
        from database.models import Bill
        v = make_visit(doc["id"], doc["patient"], doc["clinic"],
                       status=VisitStatus.billing_pending)
        r = client.post(f"/visits/{v}/close-free", data={"notes": "no charge"},
                        follow_redirects=False)
        assert r.status_code in (200, 302, 303)
        assert visit_row(v)["status"] == VisitStatus.done


# --------------------------------------------------------------------------- #
#  Ownership                                                                    #
# --------------------------------------------------------------------------- #

class TestQueueOwnership:
    """Nobody drives anybody else's queue."""

    ACTIONS = ["call", "done", "hold", "resume", "skip", "cancel", "emergency"]

    @pytest.mark.parametrize("action", ACTIONS)
    def test_another_doctor_cannot_act_on_your_visit(self, client, doc, action):
        v = make_visit(doc["id"], doc["patient"], doc["clinic"],
                       status=VisitStatus.waiting)
        before = visit_row(v)

        make_doctor(client, f"queue-intruder-{action}@test.com")
        client.post(f"/visits/{v}/{action}", follow_redirects=False)

        after = visit_row(v)
        assert after is not None, f"{action} by a stranger deleted the visit"
        assert after["status"] == before["status"], (
            f"a different doctor changed your visit via /{action}")

    def test_another_doctor_cannot_reorder_your_queue(self, client, doc):
        v = make_visit(doc["id"], doc["patient"], doc["clinic"], position=3, token=3)
        make_doctor(client, "queue-intruder-move@test.com")
        client.post(f"/visits/{v}/move", data={"new_position": 1},
                    follow_redirects=False)
        assert visit_row(v)["position"] == 3, "a stranger reordered your queue"

    def test_logged_out_cannot_touch_the_queue(self, client, doc):
        v = make_visit(doc["id"], doc["patient"], doc["clinic"])
        client.cookies.clear()
        r = client.post(f"/visits/{v}/call", follow_redirects=False)
        assert r.status_code in (302, 303, 401, 403), r.status_code
        assert visit_row(v)["status"] == VisitStatus.waiting


# --------------------------------------------------------------------------- #
#  Views and the public display                                                 #
# --------------------------------------------------------------------------- #

class TestQueueViews:

    def test_today_redirects_to_appointments(self, client, doc):
        r = client.get("/visits/today", follow_redirects=False)
        assert r.status_code in (301, 302, 303, 307, 308)

    def test_today_view_renders(self, client, doc):
        make_visit(doc["id"], doc["patient"], doc["clinic"])
        r = client.get("/visits/today-view")
        assert r.status_code == 200
        assert "Queue Patient" in r.text

    def test_today_view_accepts_a_date(self, client, doc):
        r = client.get(f"/visits/today-view?visit_date={date.today().isoformat()}")
        assert r.status_code == 200

    def test_queue_status_json(self, client, doc):
        make_visit(doc["id"], doc["patient"], doc["clinic"])
        r = client.get("/visits/queue-status")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")

    def test_public_display_needs_no_login(self, client, doc):
        db = TestSessionLocal()
        try:
            slug = db.query(Doctor).filter(Doctor.id == doc["id"]).first().slug
        finally:
            db.close()
        make_visit(doc["id"], doc["patient"], doc["clinic"])

        client.cookies.clear()
        assert client.get(f"/queue/{slug}").status_code == 200
        r = client.get(f"/queue/{slug}/status")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")

    def test_public_display_does_not_leak_patient_names(self, client, doc):
        """A waiting-room screen is visible to every stranger in the room."""
        db = TestSessionLocal()
        try:
            slug = db.query(Doctor).filter(Doctor.id == doc["id"]).first().slug
        finally:
            db.close()
        pid = make_patient(doc["id"], doc["clinic"], name="Confidential Name")
        make_visit(doc["id"], pid, doc["clinic"])

        client.cookies.clear()
        body = client.get(f"/queue/{slug}").text + client.get(f"/queue/{slug}/status").text
        assert "Confidential Name" not in body, (
            "the public queue display exposes patient names")

    def test_unknown_slug_is_not_a_500(self, client):
        client.cookies.clear()
        assert client.get("/queue/no-such-clinic").status_code in (404, 200)


class TestQueueOrderingIntegrity:
    """Positions must stay 1..N, contiguous, and confined to one clinic."""

    def test_positions_stay_one_based_and_contiguous(self, client, doc):
        ids = []
        for i in range(4):
            p = make_patient(doc["id"], doc["clinic"], name=f"Order {i}")
            ids.append(make_visit(doc["id"], p, doc["clinic"],
                                  position=i + 1, token=i + 1))

        client.post(f"/visits/{ids[3]}/move", data={"new_position": 1},
                    follow_redirects=False)
        positions = sorted(visit_row(v)["position"] for v in ids)
        assert positions == [1, 2, 3, 4], (
            f"queue positions drifted after a move: {positions}")

    def test_move_does_not_disturb_another_clinics_queue(self, client, doc):
        other_doc = make_doctor(client, "queue-move-other@test.com")
        other_clinic = clinic_of(other_doc)
        op = make_patient(other_doc, other_clinic, name="Other Queue")
        ov = make_visit(other_doc, op, other_clinic, position=5, token=5)

        login(client, doc["email"])
        p = make_patient(doc["id"], doc["clinic"], name="Mine")
        v = make_visit(doc["id"], p, doc["clinic"], position=1, token=1)
        client.post(f"/visits/{v}/move", data={"new_position": 1},
                    follow_redirects=False)

        assert visit_row(ov)["position"] == 5, (
            "a drag in one clinic renumbered another clinic's queue")

    def test_out_of_range_position_is_clamped(self, client, doc):
        """The form value is client-controlled; it must not produce nonsense."""
        v = make_visit(doc["id"], doc["patient"], doc["clinic"], position=1, token=1)
        for hostile in (-5, 0, 9999):
            client.post(f"/visits/{v}/move", data={"new_position": hostile},
                        follow_redirects=False)
            pos = visit_row(v)["position"]
            assert pos >= 1, f"new_position={hostile} produced position {pos}"
