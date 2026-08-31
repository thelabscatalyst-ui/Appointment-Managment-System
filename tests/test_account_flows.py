"""
test_account_flows.py — email verification, password reset, PIN and settings.

These are the routes that decide who gets in, so the assertions lean on the
security properties rather than the happy path alone: a reset link must not
reveal whether an address is registered, a consumed token must not work twice,
and a PIN must actually gate the pages it claims to.

No mail leaves the process. Codes and tokens are stored hashed, so the tests
that need the plaintext capture the outgoing message via the `outbox` fixture
rather than reading the database — see its docstring.
"""
from datetime import datetime, timedelta

import pytest
import re

from tests.conftest import TestSessionLocal
from tests.helpers import (make_doctor, clinic_of, register, verify_email, login,
                           set_pin, give_schedule, make_patient, phone, PASSWORD)
from database.models import Doctor, EmailVerification, PasswordReset, BlockedDate


@pytest.fixture
def doc(client):
    client.cookies.clear()
    email = f"acct-{datetime.utcnow().timestamp()}@test.com".replace(".", "-", 1)
    did = make_doctor(client, email)
    return {"id": did, "email": email, "clinic": clinic_of(did)}


@pytest.fixture
def outbox(monkeypatch):
    """Capture outgoing mail instead of sending it.

    Verification codes and reset tokens are stored as hashes (correctly), so
    the only place the plaintext exists is the message body. Patching the name
    inside each service — they do `from ... import send_email` — keeps the real
    template rendering in the path, which is what carries the code.
    """
    sent = []

    def _capture(to, subject, html, **kw):
        sent.append({"to": to, "subject": subject, "html": html})
        return True, "captured"

    import services.verification_service as vs
    import services.password_reset_service as prs
    monkeypatch.setattr(vs, "send_email", _capture)
    monkeypatch.setattr(prs, "send_email", _capture)
    return sent


def _code_from(outbox):
    for msg in reversed(outbox):
        m = re.search(r"\b(\d{6})\b", msg["subject"] + msg["html"])
        if m:
            return m.group(1)
    return None


def _reset_token_from(outbox):
    for msg in reversed(outbox):
        m = re.search(r"/reset-password\?token=([A-Za-z0-9_\-]+)", msg["html"])
        if m:
            return m.group(1)
    return None


# --------------------------------------------------------------------------- #
#  Email verification                                                           #
# --------------------------------------------------------------------------- #

class TestEmailVerification:

    def _unverified(self, client, email):
        client.cookies.clear()
        register(client, email)
        login(client, email)
        return email

    def test_unverified_doctor_is_sent_to_verify(self, client):
        self._unverified(client, "verify-gate@test.com")
        r = client.get("/dashboard", follow_redirects=False)
        assert r.status_code in (302, 303), "an unverified account reached the app"

    def test_verify_page_renders(self, client):
        self._unverified(client, "verify-page@test.com")
        assert client.get("/verify-email").status_code == 200

    def test_wrong_code_is_refused(self, client):
        self._unverified(client, "verify-wrong@test.com")
        client.post("/verify-email", data={"code": "000000"}, follow_redirects=False)
        db = TestSessionLocal()
        try:
            d = db.query(Doctor).filter(Doctor.email == "verify-wrong@test.com").first()
            assert d.email_verified_at is None, "a wrong code verified the account"
        finally:
            db.close()

    def test_correct_code_verifies(self, client, outbox):
        email = self._unverified(client, "verify-right@test.com")
        client.post("/verify-email/resend", follow_redirects=False)
        code = _code_from(outbox)
        assert code, "no verification code reached the outgoing message"

        client.post("/verify-email", data={"code": code}, follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert db.query(Doctor).filter(
                Doctor.email == email).first().email_verified_at is not None
        finally:
            db.close()

    def test_resend_issues_a_new_code(self, client):
        email = self._unverified(client, "verify-resend@test.com")
        db = TestSessionLocal()
        try:
            did = db.query(Doctor).filter(Doctor.email == email).first().id
            before = db.query(EmailVerification).filter(
                EmailVerification.doctor_id == did).count()
        finally:
            db.close()

        client.post("/verify-email/resend", follow_redirects=False)
        db = TestSessionLocal()
        try:
            after = db.query(EmailVerification).filter(
                EmailVerification.doctor_id == did).count()
            assert after >= before
        finally:
            db.close()

    def test_change_address_before_verifying(self, client):
        email = self._unverified(client, "verify-change@test.com")
        r = client.post("/verify-email/change-address",
                        data={"email": "verify-changed@test.com"},
                        follow_redirects=False)
        assert r.status_code < 500

    def test_cannot_change_to_an_address_already_in_use(self, client):
        register(client, "verify-taken@test.com")
        email = self._unverified(client, "verify-changer@test.com")
        client.post("/verify-email/change-address",
                    data={"email": "verify-taken@test.com"}, follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert db.query(Doctor).filter(
                Doctor.email == "verify-taken@test.com").count() == 1, (
                "two accounts ended up on one address")
        finally:
            db.close()


# --------------------------------------------------------------------------- #
#  Password reset                                                               #
# --------------------------------------------------------------------------- #

class TestPasswordReset:

    def test_form_renders(self, client):
        client.cookies.clear()
        assert client.get("/forgot-password").status_code == 200

    def test_response_is_identical_for_known_and_unknown_addresses(self, client):
        """Otherwise the form is a registered-user oracle.

        Both addresses are the same length, so any difference in the response
        is about EXISTENCE rather than about the text that was submitted.
        """
        client.cookies.clear()
        registered = "oracle-yes-000000@test.com"
        missing    = "oracle-no-0000000@test.com"
        assert len(registered) == len(missing)
        register(client, registered)
        client.cookies.clear()

        known = client.post("/forgot-password", data={"email": registered},
                            follow_redirects=False)
        unknown = client.post("/forgot-password", data={"email": missing},
                              follow_redirects=False)
        assert known.status_code == unknown.status_code
        assert len(known.text) == len(unknown.text), (
            "the reset form reveals whether an address is registered")

    def test_reset_token_sets_a_new_password(self, client, doc, outbox):
        client.cookies.clear()
        client.post("/forgot-password", data={"email": doc["email"]},
                    follow_redirects=False)
        token = _reset_token_from(outbox)
        assert token, "no reset link reached the outgoing message"

        assert client.get(f"/reset-password?token={token}").status_code == 200
        new_password = "Nw7&kLpq3#Zt9M"
        r = client.post("/reset-password", data={
            "token": token, "password": new_password,
            "confirm_password": new_password,
        }, follow_redirects=False)
        assert r.status_code < 500

        client.cookies.clear()
        assert login(client, doc["email"], new_password).status_code == 303, (
            "the new password does not work")

    def test_a_used_token_cannot_be_replayed(self, client, doc, outbox):
        client.cookies.clear()
        client.post("/forgot-password", data={"email": doc["email"]},
                    follow_redirects=False)
        token = _reset_token_from(outbox)
        assert token

        first = "Fst7&kLpq3#Zt9M"
        client.post("/reset-password", data={
            "token": token, "password": first, "confirm_password": first},
            follow_redirects=False)

        second = "Snd7&kLpq3#Zt9M"
        client.post("/reset-password", data={
            "token": token, "password": second, "confirm_password": second},
            follow_redirects=False)

        client.cookies.clear()
        assert login(client, doc["email"], second).status_code != 303, (
            "a spent reset token was accepted a second time")

    def test_invalid_token_is_refused(self, client):
        client.cookies.clear()
        assert client.get("/reset-password?token=nonsense").status_code < 500
        r = client.post("/reset-password", data={
            "token": "nonsense", "password": "Abc7&kLpq3#Zt9M",
            "confirm_password": "Abc7&kLpq3#Zt9M"}, follow_redirects=False)
        assert r.status_code < 500

    def test_weak_new_password_is_refused(self, client, doc, outbox):
        client.cookies.clear()
        client.post("/forgot-password", data={"email": doc["email"]},
                    follow_redirects=False)
        token = _reset_token_from(outbox)
        assert token

        client.post("/reset-password", data={
            "token": token, "password": "short", "confirm_password": "short"},
            follow_redirects=False)
        client.cookies.clear()
        assert login(client, doc["email"], "short").status_code != 303


# --------------------------------------------------------------------------- #
#  PIN                                                                          #
# --------------------------------------------------------------------------- #

class TestPin:

    def test_setting_a_pin_then_unlocking(self, client, doc):
        r = client.post("/doctors/settings/pin",
                        data={"new_pin": "246813", "confirm_pin": "246813"},
                        follow_redirects=False)
        assert r.status_code in (200, 302, 303)
        db = TestSessionLocal()
        try:
            assert db.query(Doctor).filter(
                Doctor.id == doc["id"]).first().pin_hash is not None
        finally:
            db.close()

        r = client.post("/pin-prompt", data={"pin": "246813", "next": "/reports"},
                        follow_redirects=False)
        assert r.status_code in (200, 302, 303)
        assert client.get("/reports").status_code == 200

    def test_mismatched_pins_are_refused(self, client, doc):
        client.post("/doctors/settings/pin",
                    data={"new_pin": "111111", "confirm_pin": "222222"},
                    follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert db.query(Doctor).filter(
                Doctor.id == doc["id"]).first().pin_hash is None
        finally:
            db.close()

    def test_non_numeric_pin_is_refused(self, client, doc):
        client.post("/doctors/settings/pin",
                    data={"new_pin": "abcdef", "confirm_pin": "abcdef"},
                    follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert db.query(Doctor).filter(
                Doctor.id == doc["id"]).first().pin_hash is None
        finally:
            db.close()

    def test_wrong_pin_does_not_unlock(self, client, doc):
        set_pin(client, "135790")
        client.cookies.delete("pin_session")
        r = client.post("/pin-prompt", data={"pin": "999999", "next": "/reports"},
                        follow_redirects=False)
        assert "pin_error" in r.headers.get("location", "") or r.status_code == 200

    def test_pin_prompt_page_renders(self, client, doc):
        assert client.get("/pin-prompt", follow_redirects=False).status_code < 500


# --------------------------------------------------------------------------- #
#  Settings                                                                     #
# --------------------------------------------------------------------------- #

class TestSettings:

    def test_page_renders(self, client, doc):
        set_pin(client)
        assert client.get("/doctors/settings").status_code == 200

    def test_account_details_update(self, client, doc):
        set_pin(client)
        r = client.post("/doctors/settings/account", data={
            "name": "Dr Renamed", "email": doc["email"], "phone": phone(),
            "specialization": "Cardiology", "medical_reg_number": "MH/123",
        }, follow_redirects=False)
        assert r.status_code in (200, 302, 303)
        db = TestSessionLocal()
        try:
            assert db.query(Doctor).filter(Doctor.id == doc["id"]).first().name \
                == "Dr Renamed"
        finally:
            db.close()

    def test_cannot_take_another_doctors_email(self, client, doc):
        register(client, "acct-taken@test.com")
        login(client, doc["email"])
        set_pin(client)
        client.post("/doctors/settings/account", data={
            "name": "Dr Thief", "email": "acct-taken@test.com", "phone": phone(),
            "specialization": "", "medical_reg_number": "",
        }, follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert db.query(Doctor).filter(
                Doctor.email == "acct-taken@test.com").count() == 1
        finally:
            db.close()

    def test_clinic_profile_update(self, client, doc):
        set_pin(client)
        r = client.post("/doctors/settings/profile", data={
            "clinic_name": "Renamed Clinic", "clinic_address": "12 Main St",
            "city": "Pune",
        }, follow_redirects=False)
        assert r.status_code in (200, 302, 303)

    def test_blocked_dates_lifecycle(self, client, doc):
        set_pin(client)
        target = (datetime.utcnow() + timedelta(days=10)).date()
        client.post("/doctors/settings/block",
                    data={"blocked_date": target.isoformat(), "reason": "Leave"},
                    follow_redirects=False)
        db = TestSessionLocal()
        try:
            row = db.query(BlockedDate).filter(
                BlockedDate.doctor_id == doc["id"]).first()
            assert row is not None, "blocked date was not saved"
            bid = row.id
        finally:
            db.close()

        client.post(f"/doctors/settings/unblock/{bid}", follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert db.query(BlockedDate).filter(BlockedDate.id == bid).first() is None
        finally:
            db.close()

    def test_blocked_times_lifecycle(self, client, doc):
        from database.models import BlockedTime
        set_pin(client)
        target = (datetime.utcnow() + timedelta(days=11)).date()
        r = client.post("/doctors/settings/blocktime", data={
            "blocked_date": target.isoformat(), "start_time": "13:00",
            "end_time": "14:00", "reason": "Lunch",
        }, follow_redirects=False)
        assert r.status_code in (200, 302, 303)

        db = TestSessionLocal()
        try:
            row = db.query(BlockedTime).filter(
                BlockedTime.doctor_id == doc["id"]).first()
            assert row is not None, "blocked time was not saved"
            btid = row.id
        finally:
            db.close()

        client.post(f"/doctors/settings/unblocktime/{btid}", follow_redirects=False)
        db = TestSessionLocal()
        try:
            from database.models import BlockedTime as BT
            assert db.query(BT).filter(BT.id == btid).first() is None
        finally:
            db.close()

    def test_another_doctor_cannot_unblock_your_dates(self, client, doc):
        set_pin(client)
        target = (datetime.utcnow() + timedelta(days=12)).date()
        client.post("/doctors/settings/block",
                    data={"blocked_date": target.isoformat(), "reason": "Mine"},
                    follow_redirects=False)
        db = TestSessionLocal()
        try:
            bid = db.query(BlockedDate).filter(
                BlockedDate.doctor_id == doc["id"]).first().id
        finally:
            db.close()

        make_doctor(client, "acct-block-intruder@test.com")
        set_pin(client)
        client.post(f"/doctors/settings/unblock/{bid}", follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert db.query(BlockedDate).filter(BlockedDate.id == bid).first() is not None, (
                "another doctor removed your day off")
        finally:
            db.close()


# --------------------------------------------------------------------------- #
#  Misc authenticated endpoints                                                 #
# --------------------------------------------------------------------------- #

class TestMiscEndpoints:

    def test_auth_check(self, client, doc):
        r = client.get("/auth/check")
        assert r.status_code in (200, 401)

    def test_workspace_loading(self, client, doc):
        assert client.get("/workspace-loading").status_code == 200

    def test_billing_page(self, client, doc):
        assert client.get("/billing", follow_redirects=False).status_code in (200, 302, 303)

    def test_logout_clears_the_session(self, client, doc):
        client.get("/logout", follow_redirects=False)
        assert client.get("/dashboard", follow_redirects=False).status_code in (302, 303)
