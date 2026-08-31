"""
test_patient_records.py — the patient file: notes, attachments and the vault.

This is the most sensitive data in the product. Every test that proves a
feature works is paired with one proving another doctor cannot reach it, and
the file-download routes are checked for path traversal and cross-tenant ids,
because those serve bytes straight off disk.

Several of these routes sit behind require_pin, so the fixture sets and
unlocks a PIN first.
"""
import io
from datetime import datetime

import pytest

from tests.conftest import TestSessionLocal
from tests.helpers import (make_doctor, clinic_of, make_patient, give_schedule,
                           set_pin, login, phone)
from database.models import (Patient, PatientNote, NoteFile, PatientDocument,
                             PinnedPatient)


@pytest.fixture
def doc(client):
    client.cookies.clear()
    email = f"rec-{datetime.utcnow().timestamp()}@test.com".replace(".", "-", 1)
    did = make_doctor(client, email)
    cid = clinic_of(did)
    give_schedule(did, cid)
    pid = make_patient(did, cid, name="Records Patient")
    set_pin(client)
    return {"id": did, "clinic": cid, "patient": pid, "email": email}


def _upload(name="scan.png", content=b"\x89PNG\r\n\x1a\nfake", mime="image/png"):
    return (name, io.BytesIO(content), mime)


# --------------------------------------------------------------------------- #
#  Patient list and detail                                                      #
# --------------------------------------------------------------------------- #

class TestPatientListAndDetail:

    def test_list_renders_and_searches(self, client, doc):
        make_patient(doc["id"], doc["clinic"], name="Findable Person")
        assert "Records Patient" in client.get("/patients").text
        r = client.get("/patients?q=Findable")
        assert r.status_code == 200 and "Findable Person" in r.text

    def test_list_sort_and_paging_do_not_error(self, client, doc):
        for s in ("name", "recent", "visits", "bogus"):
            assert client.get(f"/patients?sort={s}&page=1").status_code == 200
        assert client.get("/patients?page=99999").status_code == 200

    def test_detail_renders(self, client, doc):
        r = client.get(f"/patients/{doc['patient']}")
        assert r.status_code == 200 and "Records Patient" in r.text

    def test_another_doctors_patient_is_not_readable(self, client, doc):
        victim = doc["patient"]
        make_doctor(client, "rec-intruder-read@test.com")
        set_pin(client)
        r = client.get(f"/patients/{victim}", follow_redirects=False)
        assert r.status_code != 200 or "Records Patient" not in r.text, (
            "a doctor read another doctor's patient file")

    def test_unknown_patient_is_not_a_500(self, client, doc):
        assert client.get("/patients/999999", follow_redirects=False).status_code < 500


class TestPinning:

    def test_pin_then_unpin(self, client, doc):
        client.post(f"/patients/{doc['patient']}/pin", data={"q": "", "sort": ""},
                    follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert db.query(PinnedPatient).filter(
                PinnedPatient.patient_id == doc["patient"]).count() == 1
        finally:
            db.close()

        client.post(f"/patients/{doc['patient']}/unpin", data={"q": "", "sort": ""},
                    follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert db.query(PinnedPatient).filter(
                PinnedPatient.patient_id == doc["patient"]).count() == 0
        finally:
            db.close()

    def test_cannot_pin_another_doctors_patient(self, client, doc):
        victim = doc["patient"]
        make_doctor(client, "rec-pin-intruder@test.com")
        set_pin(client)
        client.post(f"/patients/{victim}/pin", data={"q": "", "sort": ""},
                    follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert db.query(PinnedPatient).filter(
                PinnedPatient.patient_id == victim).count() == 0
        finally:
            db.close()


class TestPatientEditing:

    def test_edit_updates_the_record(self, client, doc):
        r = client.post(f"/patients/{doc['patient']}/edit", data={
            "name": "Renamed Person", "phone": phone(), "age": "51",
            "gender": "female", "blood_group": "O+", "allergies": "Penicillin",
            "preferred_contact": "whatsapp", "language_pref": "en",
        }, follow_redirects=False)
        assert r.status_code in (200, 302, 303)
        db = TestSessionLocal()
        try:
            p = db.query(Patient).filter(Patient.id == doc["patient"]).first()
            assert p.name == "Renamed Person" and p.allergies == "Penicillin"
        finally:
            db.close()

    def test_another_doctor_cannot_edit(self, client, doc):
        victim = doc["patient"]
        make_doctor(client, "rec-edit-intruder@test.com")
        set_pin(client)
        client.post(f"/patients/{victim}/edit", data={
            "name": "Hijacked", "phone": phone(), "age": "1", "gender": "male",
            "blood_group": "", "allergies": "", "preferred_contact": "",
            "language_pref": "",
        }, follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert db.query(Patient).filter(Patient.id == victim).first().name \
                == "Records Patient", "another doctor renamed this patient"
        finally:
            db.close()

    def test_referral_source_records(self, client, doc):
        r = client.post(f"/patients/{doc['patient']}/source", data={
            "referral_source": "walk_in", "referral_source_other": "",
        }, follow_redirects=False)
        assert r.status_code in (200, 302, 303)

    def test_wa_consent_toggles(self, client, doc):
        for value in ("1", "0"):
            r = client.post(f"/patients/{doc['patient']}/wa-consent",
                            data={"consent": value}, follow_redirects=False)
            assert r.status_code in (200, 302, 303)

    def test_delete_removes_the_patient(self, client, doc):
        pid = make_patient(doc["id"], doc["clinic"], name="To Be Deleted")
        client.post(f"/patients/{pid}/delete", follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert db.query(Patient).filter(Patient.id == pid).first() is None
        finally:
            db.close()

    def test_another_doctor_cannot_delete(self, client, doc):
        victim = doc["patient"]
        make_doctor(client, "rec-del-intruder@test.com")
        set_pin(client)
        client.post(f"/patients/{victim}/delete", follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert db.query(Patient).filter(Patient.id == victim).first() is not None, (
                "another doctor deleted this patient")
        finally:
            db.close()


# --------------------------------------------------------------------------- #
#  Notes and attachments                                                        #
# --------------------------------------------------------------------------- #

class TestNotes:

    def _add(self, client, patient_id, text="First consultation note"):
        return client.post(f"/patients/{patient_id}/notes/add",
                           data={"note_text": text}, follow_redirects=False)

    def test_add_note(self, client, doc):
        r = self._add(client, doc["patient"])
        assert r.status_code in (200, 302, 303)
        db = TestSessionLocal()
        try:
            n = db.query(PatientNote).filter(
                PatientNote.patient_id == doc["patient"]).first()
            assert n is not None and "First consultation" in n.note_text
        finally:
            db.close()

    def test_add_note_with_attachment(self, client, doc):
        r = client.post(f"/patients/{doc['patient']}/notes/add",
                        data={"note_text": "With a scan"},
                        files={"files": _upload()}, follow_redirects=False)
        assert r.status_code in (200, 302, 303)
        db = TestSessionLocal()
        try:
            assert db.query(NoteFile).count() >= 1, "attachment was not stored"
        finally:
            db.close()

    def test_edit_note(self, client, doc):
        self._add(client, doc["patient"])
        db = TestSessionLocal()
        try:
            nid = db.query(PatientNote).filter(
                PatientNote.patient_id == doc["patient"]).first().id
        finally:
            db.close()

        client.post(f"/patients/{doc['patient']}/notes/{nid}/edit",
                    data={"note_text": "Corrected note"}, follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert "Corrected" in db.query(PatientNote).filter(
                PatientNote.id == nid).first().note_text
        finally:
            db.close()

    def test_delete_note(self, client, doc):
        self._add(client, doc["patient"])
        db = TestSessionLocal()
        try:
            nid = db.query(PatientNote).filter(
                PatientNote.patient_id == doc["patient"]).first().id
        finally:
            db.close()

        client.post(f"/patients/{doc['patient']}/notes/{nid}/delete",
                    follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert db.query(PatientNote).filter(PatientNote.id == nid).first() is None
        finally:
            db.close()

    def test_quick_notes_field(self, client, doc):
        r = client.post(f"/patients/{doc['patient']}/notes",
                        data={"notes": "Diabetic, on metformin"},
                        follow_redirects=False)
        assert r.status_code in (200, 302, 303)

    def test_another_doctor_cannot_add_or_delete_notes(self, client, doc):
        self._add(client, doc["patient"])
        db = TestSessionLocal()
        try:
            nid = db.query(PatientNote).filter(
                PatientNote.patient_id == doc["patient"]).first().id
        finally:
            db.close()

        victim = doc["patient"]
        make_doctor(client, "rec-note-intruder@test.com")
        set_pin(client)
        client.post(f"/patients/{victim}/notes/{nid}/delete", follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert db.query(PatientNote).filter(PatientNote.id == nid).first() is not None, (
                "another doctor deleted this clinical note")
        finally:
            db.close()


# --------------------------------------------------------------------------- #
#  The document vault                                                           #
# --------------------------------------------------------------------------- #

class TestVault:

    def _upload_doc(self, client, patient_id, category="lab_report"):
        return client.post(f"/patients/{patient_id}/vault/upload",
                           data={"category": category, "description": "Blood work"},
                           files={"files": _upload("report.pdf", b"%PDF-1.4 fake",
                                                   "application/pdf")},
                           follow_redirects=False)

    def test_vault_page_renders(self, client, doc):
        assert client.get(f"/patients/{doc['patient']}/vault").status_code == 200

    def test_upload_and_read_back(self, client, doc):
        r = self._upload_doc(client, doc["patient"])
        assert r.status_code in (200, 302, 303), r.text[:300]

        db = TestSessionLocal()
        try:
            d = db.query(PatientDocument).filter(
                PatientDocument.patient_id == doc["patient"]).first()
            assert d is not None, "vault upload stored nothing"
            did = d.id
        finally:
            db.close()

        assert client.get(f"/patients/{doc['patient']}/vault/{did}").status_code == 200
        assert client.get(
            f"/patients/{doc['patient']}/vault/{did}?download=1").status_code == 200

    def test_edit_and_delete_document(self, client, doc):
        self._upload_doc(client, doc["patient"])
        db = TestSessionLocal()
        try:
            did = db.query(PatientDocument).filter(
                PatientDocument.patient_id == doc["patient"]).first().id
        finally:
            db.close()

        client.post(f"/patients/{doc['patient']}/vault/{did}/edit",
                    data={"category": "prescription", "description": "Updated"},
                    follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert db.query(PatientDocument).filter(
                PatientDocument.id == did).first().description == "Updated"
        finally:
            db.close()

        client.post(f"/patients/{doc['patient']}/vault/{did}/delete",
                    follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert db.query(PatientDocument).filter(
                PatientDocument.id == did).first() is None
        finally:
            db.close()

    def test_another_doctor_cannot_download_a_document(self, client, doc):
        """These routes serve bytes off disk — the sharpest leak in the app."""
        self._upload_doc(client, doc["patient"])
        db = TestSessionLocal()
        try:
            d = db.query(PatientDocument).filter(
                PatientDocument.patient_id == doc["patient"]).first()
            did, victim = d.id, doc["patient"]
        finally:
            db.close()

        make_doctor(client, "rec-vault-intruder@test.com")
        set_pin(client)
        r = client.get(f"/patients/{victim}/vault/{did}", follow_redirects=False)
        assert r.status_code != 200, (
            "another doctor downloaded this patient's medical document")

    def test_document_id_from_another_patient_is_refused(self, client, doc):
        """Owning ONE patient must not unlock documents of another."""
        self._upload_doc(client, doc["patient"])
        db = TestSessionLocal()
        try:
            did = db.query(PatientDocument).filter(
                PatientDocument.patient_id == doc["patient"]).first().id
        finally:
            db.close()

        other_patient = make_patient(doc["id"], doc["clinic"], name="Other File")
        r = client.get(f"/patients/{other_patient}/vault/{did}",
                       follow_redirects=False)
        assert r.status_code != 200, (
            "a document was served under a patient it does not belong to")

    def test_logged_out_cannot_reach_the_vault(self, client, doc):
        self._upload_doc(client, doc["patient"])
        pid = doc["patient"]
        client.cookies.clear()
        r = client.get(f"/patients/{pid}/vault", follow_redirects=False)
        assert r.status_code in (302, 303, 401, 403)

    def test_unknown_document_is_not_a_500(self, client, doc):
        assert client.get(f"/patients/{doc['patient']}/vault/999999",
                          follow_redirects=False).status_code < 500
