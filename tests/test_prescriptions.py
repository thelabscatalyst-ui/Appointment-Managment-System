"""
test_prescriptions.py — writing, editing, printing and deleting prescriptions.

A prescription is a medical record with the doctor's name and registration
number on it, so the tests care about two things above all: that the drug lines
survive a round trip intact, and that no doctor can read, alter or delete
another doctor's prescriptions.
"""
from datetime import datetime

import pytest

from tests.conftest import TestSessionLocal
from tests.helpers import (make_doctor, clinic_of, make_patient, make_visit,
                           give_schedule, login, phone)
from database.models import Prescription, PrescriptionItem, VisitStatus


@pytest.fixture
def doc(client):
    client.cookies.clear()
    email = f"rx-{datetime.utcnow().timestamp()}@test.com".replace(".", "-", 1)
    did = make_doctor(client, email)
    cid = clinic_of(did)
    give_schedule(did, cid)
    pid = make_patient(did, cid, name="Rx Patient")
    return {"id": did, "clinic": cid, "patient": pid, "email": email}


def _create(client, patient_id, *, diagnosis="Viral fever",
            drugs=(("Paracetamol", "500mg", "1-0-1", "5 days"),), visit_id=None):
    data = {
        "patient_id": patient_id, "diagnosis": diagnosis,
        "advice": "Rest and fluids", "follow_up": "",
        "drug_name": [d[0] for d in drugs],
        "dosage": [d[1] for d in drugs],
        "frequency": [d[2] for d in drugs],
        "duration": [d[3] for d in drugs],
        "instructions": ["After food"] * len(drugs),
        "notes": [""] * len(drugs),
    }
    if visit_id:
        data["visit_id"] = visit_id
    return client.post("/prescriptions/new", data=data, follow_redirects=False)


def rx_for(doctor_id):
    db = TestSessionLocal()
    try:
        return db.query(Prescription).filter(
            Prescription.doctor_id == doctor_id).all()
    finally:
        db.close()


def rx_items(rx_id):
    db = TestSessionLocal()
    try:
        return [(i.drug_name, i.dosage, i.frequency, i.duration) for i in
                db.query(PrescriptionItem).filter(
                    PrescriptionItem.prescription_id == rx_id).all()]
    finally:
        db.close()


class TestCreatePrescription:

    def test_new_form_renders(self, client, doc):
        r = client.get(f"/prescriptions/new?patient_id={doc['patient']}")
        assert r.status_code == 200

    def test_create_saves_header_and_drug_lines(self, client, doc):
        r = _create(client, doc["patient"], drugs=(
            ("Paracetamol", "500mg", "1-0-1", "5 days"),
            ("Azithromycin", "250mg", "0-0-1", "3 days"),
        ))
        assert r.status_code in (200, 302, 303), r.text[:300]

        all_rx = rx_for(doc["id"])
        assert len(all_rx) == 1, f"expected one prescription, got {len(all_rx)}"
        items = rx_items(all_rx[0].id)
        assert len(items) == 2, f"drug lines lost: {items}"
        assert ("Paracetamol", "500mg", "1-0-1", "5 days") in items

    def test_blank_drug_rows_are_dropped(self, client, doc):
        """The form ships spare empty rows; they must not become drug lines."""
        client.post("/prescriptions/new", data={
            "patient_id": doc["patient"], "diagnosis": "Checkup",
            "advice": "", "follow_up": "",
            "drug_name": ["Paracetamol", "", ""],
            "dosage": ["500mg", "", ""],
            "frequency": ["1-0-1", "", ""],
            "duration": ["3 days", "", ""],
            "instructions": ["", "", ""],
            "notes": ["", "", ""],
        }, follow_redirects=False)

        all_rx = rx_for(doc["id"])
        assert len(all_rx) == 1
        items = rx_items(all_rx[0].id)
        assert len(items) == 1, f"empty rows were saved as drugs: {items}"

    def test_cannot_prescribe_for_another_doctors_patient(self, client, doc):
        victim_patient = doc["patient"]
        intruder = make_doctor(client, "rx-intruder@test.com")
        _create(client, victim_patient)
        assert rx_for(intruder) == [], (
            "a doctor wrote a prescription against another doctor's patient")

    def test_prescription_can_attach_to_a_visit(self, client, doc):
        v = make_visit(doc["id"], doc["patient"], doc["clinic"],
                       status=VisitStatus.serving)
        _create(client, doc["patient"], visit_id=v)
        all_rx = rx_for(doc["id"])
        assert len(all_rx) == 1


class TestReadPrescription:

    def _one(self, client, doc):
        _create(client, doc["patient"])
        return rx_for(doc["id"])[0].id

    def test_detail_renders(self, client, doc):
        rx = self._one(client, doc)
        r = client.get(f"/prescriptions/{rx}")
        assert r.status_code == 200
        assert "Paracetamol" in r.text

    def test_print_view_renders(self, client, doc):
        rx = self._one(client, doc)
        r = client.get(f"/prescriptions/{rx}/print")
        assert r.status_code == 200
        assert "Paracetamol" in r.text

    def test_patient_prescription_list_renders(self, client, doc):
        self._one(client, doc)
        r = client.get(f"/patients/{doc['patient']}/prescriptions")
        assert r.status_code == 200

    def test_another_doctor_cannot_read_it(self, client, doc):
        rx = self._one(client, doc)
        make_doctor(client, "rx-reader@test.com")
        for path in (f"/prescriptions/{rx}", f"/prescriptions/{rx}/print",
                     f"/prescriptions/{rx}/edit"):
            r = client.get(path, follow_redirects=False)
            assert r.status_code != 200 or "Paracetamol" not in r.text, (
                f"{path} leaked another doctor's prescription")

    def test_logged_out_cannot_read_it(self, client, doc):
        rx = self._one(client, doc)
        client.cookies.clear()
        r = client.get(f"/prescriptions/{rx}", follow_redirects=False)
        assert r.status_code in (302, 303, 401, 403)


class TestEditAndDelete:

    def _one(self, client, doc):
        _create(client, doc["patient"])
        return rx_for(doc["id"])[0].id

    def test_edit_replaces_the_drug_lines(self, client, doc):
        rx = self._one(client, doc)
        r = client.post(f"/prescriptions/{rx}/edit", data={
            "diagnosis": "Bacterial infection", "advice": "Complete the course",
            "follow_up": "",
            "drug_name": ["Amoxicillin"], "dosage": ["625mg"],
            "frequency": ["1-1-1"], "duration": ["7 days"],
            "instructions": [""], "notes": [""],
        }, follow_redirects=False)
        assert r.status_code in (200, 302, 303)

        items = rx_items(rx)
        assert len(items) == 1, f"old drug lines were not replaced: {items}"
        assert items[0][0] == "Amoxicillin"

    def test_autosave_accepts_a_partial_draft(self, client, doc):
        rx = self._one(client, doc)
        r = client.post(f"/prescriptions/{rx}/autosave",
                        json={"diagnosis": "Draft in progress"},
                        follow_redirects=False)
        assert r.status_code in (200, 204), r.text[:200]

    def test_another_doctor_cannot_edit_it(self, client, doc):
        rx = self._one(client, doc)
        make_doctor(client, "rx-editor@test.com")
        client.post(f"/prescriptions/{rx}/edit", data={
            "diagnosis": "Tampered", "advice": "", "follow_up": "",
            "drug_name": ["Nothing"], "dosage": [""], "frequency": [""],
            "duration": [""], "instructions": [""], "notes": [""],
        }, follow_redirects=False)

        items = rx_items(rx)
        assert items and items[0][0] == "Paracetamol", (
            f"another doctor rewrote the prescription: {items}")

    def test_another_doctor_cannot_delete_it(self, client, doc):
        rx = self._one(client, doc)
        make_doctor(client, "rx-deleter@test.com")
        client.post(f"/prescriptions/{rx}/delete", follow_redirects=False)

        db = TestSessionLocal()
        try:
            assert db.query(Prescription).filter(
                Prescription.id == rx).first() is not None, (
                "another doctor deleted this prescription")
        finally:
            db.close()

    def test_owner_can_delete_it(self, client, doc):
        rx = self._one(client, doc)
        client.post(f"/prescriptions/{rx}/delete", follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert db.query(Prescription).filter(Prescription.id == rx).first() is None
            assert db.query(PrescriptionItem).filter(
                PrescriptionItem.prescription_id == rx).count() == 0, (
                "drug lines were orphaned when the prescription was deleted")
        finally:
            db.close()

    def test_unknown_id_is_not_a_500(self, client, doc):
        for path in ("/prescriptions/999999", "/prescriptions/999999/print",
                     "/prescriptions/999999/edit"):
            assert client.get(path, follow_redirects=False).status_code < 500
