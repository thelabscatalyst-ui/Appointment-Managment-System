"""
test_invite_workflows.py — the clinic doctor-invite journey, end to end.

Covers the two entry points an invited doctor can take, and every way each can
go wrong:

  * they already have an account  -> log in, then accept
  * they have no account at all   -> register from the invite link

The second path is the one that kept breaking. It is not a single endpoint: it
spans /clinic/doctor-invite/{token} (GET), /register (GET, with the token in a
query string), and /register (POST, with the token in a hidden field). A token
dropped at any hop silently produces a solo account for someone who believed
they were joining a clinic, which looks like "the invite link stopped working".

No email is sent anywhere in here: invites are created directly in the
database, and conftest blanks RESEND_API_KEY for the whole suite.
"""
import re
from datetime import datetime, timedelta

import pytest

from tests.conftest import TestSessionLocal
from database.models import (Doctor, Clinic, ClinicDoctor, ClinicDoctorInvite,
                             PlanType)

PASSWORD = "Kv9$mPq2#Zx8L"
_phone_seq = [9700000000]


def _phone():
    _phone_seq[0] += 1
    return str(_phone_seq[0])


# --------------------------------------------------------------------------- #
#  Helpers                                                                      #
# --------------------------------------------------------------------------- #

def register(client, email, *, name="Dr Invitee", clinic_name="Test Clinic",
             account_type="solo", clinic_invite="", also_own_practice=None,
             password=PASSWORD, phone=None, city="Mumbai"):
    """POST /register exactly as the form does."""
    data = {
        "name": name, "email": email, "phone": phone or _phone(),
        "password": password, "clinic_name": clinic_name, "city": city,
        "specialization": "General", "clinic_invite": clinic_invite,
        "account_type": account_type,
    }
    if also_own_practice:
        data["also_own_practice"] = "1"
    return client.post("/register", data=data, follow_redirects=False)


def verify(email):
    """Mark the address confirmed — email verification has its own tests."""
    db = TestSessionLocal()
    try:
        d = db.query(Doctor).filter(Doctor.email == email.strip().lower()).first()
        if d and not d.email_verified_at:
            d.email_verified_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()


def login(client, email, password=PASSWORD):
    return client.post("/login", data={"email": email, "password": password},
                       follow_redirects=False)


def make_clinic_owner(client, email, clinic_name="Host Clinic", seats=5):
    """A doctor on a real Clinic plan, entitled to invite."""
    register(client, email, account_type="clinic", clinic_name=clinic_name)
    verify(email)
    db = TestSessionLocal()
    try:
        owner = db.query(Doctor).filter(Doctor.email == email).first()
        clinic = db.query(Clinic).filter(Clinic.owner_doctor_id == owner.id).first()
        clinic.plan_type = "clinic"
        clinic.max_doctors = seats
        clinic.plan_expires_at = datetime.utcnow() + timedelta(days=30)
        db.commit()
        return owner.id, clinic.id
    finally:
        db.close()


def make_invite(clinic_id, email, *, days=7, used=False):
    """An invite row, created directly — no mail involved."""
    import secrets
    token = secrets.token_urlsafe(32)
    db = TestSessionLocal()
    try:
        db.add(ClinicDoctorInvite(
            clinic_id=clinic_id, email=email.lower().strip(), token=token,
            expires_at=datetime.utcnow() + timedelta(days=days),
            used_at=datetime.utcnow() if used else None,
        ))
        db.commit()
    finally:
        db.close()
    return token


def memberships(email):
    db = TestSessionLocal()
    try:
        d = db.query(Doctor).filter(Doctor.email == email).first()
        if not d:
            return []
        return [(m.clinic_id, m.role, m.is_active) for m in
                db.query(ClinicDoctor).filter(ClinicDoctor.doctor_id == d.id).all()]
    finally:
        db.close()


def invite_state(token):
    db = TestSessionLocal()
    try:
        i = db.query(ClinicDoctorInvite).filter(
            ClinicDoctorInvite.token == token).first()
        return None if i is None else {"used": i.used_at is not None,
                                       "email": i.email,
                                       "clinic_id": i.clinic_id}
    finally:
        db.close()


def doctor_row(email):
    db = TestSessionLocal()
    try:
        d = db.query(Doctor).filter(Doctor.email == email).first()
        if d is None:
            return None
        return {"id": d.id, "trial_ends_at": d.trial_ends_at,
                "clinic_name": d.clinic_name, "verified": d.email_verified_at}
    finally:
        db.close()


# --------------------------------------------------------------------------- #
#  1. The invite landing page                                                   #
# --------------------------------------------------------------------------- #

class TestInviteLandingPage:

    def test_logged_out_visitor_is_offered_both_routes(self, client):
        """Someone with no account must be able to get in from here.

        Whichever they pick, the token has to travel with them — a Create
        account link without it lands them on a plain signup form and they end
        up with a solo practice instead of a clinic seat.
        """
        client.cookies.clear()
        _, clinic_id = make_clinic_owner(client, "land-owner@test.com")
        token = make_invite(clinic_id, "land-new@test.com")

        client.cookies.clear()
        r = client.get(f"/clinic/doctor-invite/{token}")
        assert r.status_code == 200
        assert f"/register?clinic_invite={token}" in r.text, (
            "no registration route carrying the invite token")
        assert f"/login?next=/clinic/doctor-invite/{token}" in r.text, (
            "no login route that returns to this invite")

    def test_expired_invite_is_refused(self, client):
        client.cookies.clear()
        _, clinic_id = make_clinic_owner(client, "exp-owner@test.com")
        token = make_invite(clinic_id, "exp-new@test.com", days=-1)
        client.cookies.clear()
        assert client.get(f"/clinic/doctor-invite/{token}").status_code == 410

    def test_used_invite_is_refused(self, client):
        client.cookies.clear()
        _, clinic_id = make_clinic_owner(client, "used-owner@test.com")
        token = make_invite(clinic_id, "used-new@test.com", used=True)
        client.cookies.clear()
        assert client.get(f"/clinic/doctor-invite/{token}").status_code == 410

    def test_unknown_token_is_refused(self, client):
        client.cookies.clear()
        assert client.get("/clinic/doctor-invite/nope-not-a-token").status_code == 410


# --------------------------------------------------------------------------- #
#  2. Registering from an invite — the brand-new doctor                         #
# --------------------------------------------------------------------------- #

class TestRegisterFromInvite:

    def test_register_page_carries_the_token_into_the_form(self, client):
        """GET /register?clinic_invite=… must plant it as a hidden field.

        If it does not, the POST arrives without a token and the invited
        doctor is silently given their own solo practice — the reported
        "registering as new forgets that there was an invite".
        """
        client.cookies.clear()
        _, clinic_id = make_clinic_owner(client, "carry-owner@test.com")
        token = make_invite(clinic_id, "carry-new@test.com")

        client.cookies.clear()
        r = client.get(f"/register?clinic_invite={token}")
        assert r.status_code == 200
        assert re.search(
            rf'name="clinic_invite"[^>]*value="{re.escape(token)}"', r.text), (
            "the register form does not carry the invite token")

    def test_register_page_offers_the_own_practice_choice(self, client):
        """The invitee must be ASKED, not assumed either way."""
        client.cookies.clear()
        _, clinic_id = make_clinic_owner(client, "ask-owner@test.com")
        token = make_invite(clinic_id, "ask-new@test.com")

        client.cookies.clear()
        r = client.get(f"/register?clinic_invite={token}")
        assert 'name="also_own_practice"' in r.text, (
            "no way to say whether they also run their own practice")

    def test_joining_without_own_practice(self, client):
        """Clinic seat only: no personal trial, no second clinic."""
        client.cookies.clear()
        _, clinic_id = make_clinic_owner(client, "join1-owner@test.com")
        token = make_invite(clinic_id, "join1-new@test.com")

        client.cookies.clear()
        r = register(client, "join1-new@test.com", clinic_invite=token)
        assert r.status_code in (200, 302, 303), r.text[:300]

        mem = memberships("join1-new@test.com")
        assert (clinic_id, "associate", True) in mem, f"did not join: {mem}"
        assert len(mem) == 1, f"unexpected extra membership: {mem}"
        assert doctor_row("join1-new@test.com")["trial_ends_at"] is None, (
            "clinic-funded seat must not also get a personal trial")
        assert invite_state(token)["used"] is True

    def test_joining_with_own_practice(self, client):
        """Both: a clinic seat AND their own practice on a trial."""
        client.cookies.clear()
        _, clinic_id = make_clinic_owner(client, "join2-owner@test.com")
        token = make_invite(clinic_id, "join2-new@test.com")

        client.cookies.clear()
        register(client, "join2-new@test.com", clinic_invite=token,
                 also_own_practice=True, clinic_name="My Own Practice")

        mem = memberships("join2-new@test.com")
        roles = {r for _, r, _ in mem}
        assert roles == {"associate", "owner"}, f"expected both roles: {mem}"
        assert doctor_row("join2-new@test.com")["trial_ends_at"] is not None, (
            "own practice must come with its own trial")

    def test_email_must_match_the_invite(self, client):
        client.cookies.clear()
        _, clinic_id = make_clinic_owner(client, "match-owner@test.com")
        token = make_invite(clinic_id, "match-invited@test.com")

        client.cookies.clear()
        r = register(client, "match-other@test.com", clinic_invite=token)
        assert r.status_code == 400
        assert memberships("match-other@test.com") == []
        assert invite_state(token)["used"] is False, "invite burned by a stranger"

    def test_bad_token_does_not_silently_create_a_solo_account(self, client):
        """The worst failure mode: they think they joined, and did not."""
        client.cookies.clear()
        r = register(client, "badtok-new@test.com", clinic_invite="garbage-token")
        assert r.status_code == 400
        assert doctor_row("badtok-new@test.com") is None, (
            "an account was created despite the invite being invalid")

    def test_expired_token_at_submit_is_refused(self, client):
        client.cookies.clear()
        _, clinic_id = make_clinic_owner(client, "expsub-owner@test.com")
        token = make_invite(clinic_id, "expsub-new@test.com", days=-1)
        client.cookies.clear()
        r = register(client, "expsub-new@test.com", clinic_invite=token)
        assert r.status_code == 400
        assert doctor_row("expsub-new@test.com") is None


# --------------------------------------------------------------------------- #
#  3. Recovering from a mistake mid-registration                                #
# --------------------------------------------------------------------------- #

class TestInviteSurvivesAValidationError:
    """One typo must not cost the invitee their clinic seat.

    Every re-render of the signup form has to carry the invite forward: the
    hidden token AND the invite-mode UI. Losing the UI turns the page back
    into an ordinary signup offering "Solo Doctor / Clinic Account", which is
    exactly the reported "no option to ask whether you want a clinic — the
    account is made forcefully".
    """

    @staticmethod
    def _setup(client, owner_email, invitee_email):
        client.cookies.clear()
        _, clinic_id = make_clinic_owner(client, owner_email)
        token = make_invite(clinic_id, invitee_email)
        client.cookies.clear()
        return clinic_id, token

    def test_weak_password_keeps_the_token(self, client):
        clinic_id, token = self._setup(
            client, "weak-owner@test.com", "weak-new@test.com")
        r = register(client, "weak-new@test.com", clinic_invite=token,
                     password="short")
        assert r.status_code == 400
        assert re.search(
            rf'name="clinic_invite"[^>]*value="{re.escape(token)}"', r.text), (
            "the invite token was dropped from the retry form")

    def test_weak_password_keeps_the_invite_ui(self, client):
        clinic_id, token = self._setup(
            client, "weakui-owner@test.com", "weakui-new@test.com")
        r = register(client, "weakui-new@test.com", clinic_invite=token,
                     password="short")
        assert 'name="also_own_practice"' in r.text, (
            "the own-practice choice vanished after a validation error")
        assert 'onclick="setMode(' not in r.text, (
            "the Solo/Clinic selector reappeared on an invited signup")

    def test_retry_after_an_error_still_joins(self, client):
        """The whole point: the second attempt must work."""
        clinic_id, token = self._setup(
            client, "retry-owner@test.com", "retry-new@test.com")
        bad = register(client, "retry-new@test.com", clinic_invite=token,
                       password="short")
        assert bad.status_code == 400
        assert invite_state(token)["used"] is False, (
            "a failed attempt consumed the invite")

        register(client, "retry-new@test.com", clinic_invite=token)
        assert (clinic_id, "associate", True) in memberships("retry-new@test.com")

    def test_duplicate_phone_keeps_the_invite_ui(self, client):
        clinic_id, token = self._setup(
            client, "dup-owner@test.com", "dup-new@test.com")
        shared = _phone()
        register(client, "dup-other@test.com", phone=shared)
        r = register(client, "dup-new@test.com", clinic_invite=token,
                     phone=shared)
        assert r.status_code == 400
        assert 'name="also_own_practice"' in r.text
        assert invite_state(token)["used"] is False


# --------------------------------------------------------------------------- #
#  4. The invitee who already has an account                                    #
# --------------------------------------------------------------------------- #

class TestExistingAccountAcceptsInvite:

    def test_login_returns_to_the_invite_and_accepts(self, client):
        client.cookies.clear()
        _, clinic_id = make_clinic_owner(client, "ex-owner@test.com")
        register(client, "ex-doc@test.com")
        verify("ex-doc@test.com")
        token = make_invite(clinic_id, "ex-doc@test.com")

        client.cookies.clear()
        r = login(client, "ex-doc@test.com")
        assert r.status_code == 303
        page = client.get(f"/clinic/doctor-invite/{token}")
        assert page.status_code == 200

        r = client.post(f"/clinic/doctor-invite/{token}", follow_redirects=False)
        assert r.status_code in (200, 302, 303)
        assert (clinic_id, "associate", True) in memberships("ex-doc@test.com")
        assert invite_state(token)["used"] is True

    def test_a_different_logged_in_doctor_cannot_take_the_seat(self, client):
        client.cookies.clear()
        _, clinic_id = make_clinic_owner(client, "steal-owner@test.com")
        register(client, "steal-invited@test.com"); verify("steal-invited@test.com")
        register(client, "steal-other@test.com");   verify("steal-other@test.com")
        token = make_invite(clinic_id, "steal-invited@test.com")

        client.cookies.clear()
        login(client, "steal-other@test.com")
        client.post(f"/clinic/doctor-invite/{token}", follow_redirects=False)

        assert memberships("steal-other@test.com") == [
            m for m in memberships("steal-other@test.com") if m[0] != clinic_id
        ], "a doctor accepted an invite addressed to someone else"
        assert invite_state(token)["used"] is False

    def test_owning_a_practice_does_not_block_joining(self, client):
        """The common real case: a doctor with their own clinic takes shifts."""
        client.cookies.clear()
        _, clinic_id = make_clinic_owner(client, "both-owner@test.com")
        register(client, "both-doc@test.com", clinic_name="Own Practice")
        verify("both-doc@test.com")
        token = make_invite(clinic_id, "both-doc@test.com")

        client.cookies.clear()
        login(client, "both-doc@test.com")
        client.post(f"/clinic/doctor-invite/{token}", follow_redirects=False)

        mem = memberships("both-doc@test.com")
        assert {r for _, r, _ in mem} == {"owner", "associate"}, mem

    def test_already_a_member_is_told_so(self, client):
        client.cookies.clear()
        _, clinic_id = make_clinic_owner(client, "already-owner@test.com")
        register(client, "already-doc@test.com"); verify("already-doc@test.com")
        token = make_invite(clinic_id, "already-doc@test.com")

        client.cookies.clear()
        login(client, "already-doc@test.com")
        client.post(f"/clinic/doctor-invite/{token}", follow_redirects=False)

        token2 = make_invite(clinic_id, "already-doc@test.com")
        page = client.get(f"/clinic/doctor-invite/{token2}")
        assert page.status_code == 200
        mem = [m for m in memberships("already-doc@test.com") if m[0] == clinic_id]
        assert len(mem) == 1, f"joined the same clinic twice: {mem}"


# --------------------------------------------------------------------------- #
#  5. Seat limits and lifecycle                                                 #
# --------------------------------------------------------------------------- #

class TestSeatLimitsAndLifecycle:

    def test_seat_cap_is_enforced_at_accept_time(self, client):
        """Checking only at send time lets an old invite overfill the clinic."""
        client.cookies.clear()
        _, clinic_id = make_clinic_owner(client, "cap-owner@test.com", seats=2)
        token = make_invite(clinic_id, "cap-late@test.com")

        # Fill the last seat behind the invite's back.
        register(client, "cap-filler@test.com"); verify("cap-filler@test.com")
        db = TestSessionLocal()
        try:
            filler = db.query(Doctor).filter(Doctor.email == "cap-filler@test.com").first()
            db.add(ClinicDoctor(clinic_id=clinic_id, doctor_id=filler.id,
                                role="associate", is_active=True))
            db.commit()
        finally:
            db.close()

        client.cookies.clear()
        register(client, "cap-late@test.com", clinic_invite=token)
        assert (clinic_id, "associate", True) not in memberships("cap-late@test.com"), (
            "an invite accepted past the seat cap")

    def test_a_new_invite_supersedes_the_previous_one(self, client):
        client.cookies.clear()
        owner_id, clinic_id = make_clinic_owner(client, "sup-owner@test.com")
        first = make_invite(clinic_id, "sup-new@test.com")

        # Owner re-invites the same address through the real route.
        login(client, "sup-owner@test.com")
        client.post("/clinic/admin/auth", data={"password": PASSWORD},
                    follow_redirects=False)
        client.post("/clinic/admin/doctors/invite",
                    data={"invite_email": "sup-new@test.com"},
                    follow_redirects=False)

        st = invite_state(first)
        assert st["used"] is True, "the superseded invite is still live"
        client.cookies.clear()
        assert client.get(f"/clinic/doctor-invite/{first}").status_code == 410

    def test_accepting_does_not_grant_owner_powers(self, client):
        """A new associate is an associate everywhere it matters."""
        client.cookies.clear()
        _, clinic_id = make_clinic_owner(client, "pow-owner@test.com")
        token = make_invite(clinic_id, "pow-new@test.com")

        client.cookies.clear()
        register(client, "pow-new@test.com", clinic_invite=token)
        verify("pow-new@test.com")
        login(client, "pow-new@test.com")

        for path in ("/income", "/reports", "/expenses"):
            r = client.get(path, follow_redirects=False)
            assert r.status_code in (302, 303), (
                f"{path} reachable by a freshly joined associate")
        assert client.get("/clinic/admin", follow_redirects=False).status_code in (302, 303, 403)


# --------------------------------------------------------------------------- #
#  6. The joined doctor can actually work                                       #
# --------------------------------------------------------------------------- #

class TestJoinedDoctorCanWork:

    def test_clinic_funds_access_without_a_personal_plan(self, client):
        """No trial of their own — the clinic's plan is the entitlement."""
        client.cookies.clear()
        _, clinic_id = make_clinic_owner(client, "work-owner@test.com")
        token = make_invite(clinic_id, "work-new@test.com")

        client.cookies.clear()
        register(client, "work-new@test.com", clinic_invite=token)
        verify("work-new@test.com")
        login(client, "work-new@test.com")

        r = client.get("/dashboard", follow_redirects=False)
        assert r.status_code == 200, (
            f"a clinic-funded associate cannot reach the dashboard: "
            f"{r.status_code} -> {r.headers.get('location')}")

    def test_dual_role_doctor_gets_a_switcher(self, client):
        client.cookies.clear()
        _, clinic_id = make_clinic_owner(client, "dual-owner@test.com")
        token = make_invite(clinic_id, "dual-new@test.com")

        client.cookies.clear()
        register(client, "dual-new@test.com", clinic_invite=token,
                 also_own_practice=True, clinic_name="Dual Own Practice")
        verify("dual-new@test.com")
        login(client, "dual-new@test.com")

        body = client.get("/dashboard").text
        assert 'action="/clinic/switch"' in body, (
            "a doctor with two clinics has no way to switch between them")


# --------------------------------------------------------------------------- #
#  7. The invitee is ASKED, not assumed                                         #
# --------------------------------------------------------------------------- #

class TestInviteeIsAskedAboutTheirOwnPractice:
    """"Currently the new account is made forcefully" — the reported symptom.

    An invited doctor may or may not also run their own practice. Both are
    normal, so the form has to ask, keep the answer across a failed submit,
    and let them name the practice they opted into.
    """

    @staticmethod
    def _setup(client, owner_email, invitee_email):
        client.cookies.clear()
        _, clinic_id = make_clinic_owner(client, owner_email)
        token = make_invite(clinic_id, invitee_email)
        client.cookies.clear()
        return clinic_id, token

    def test_ticked_choice_survives_a_validation_error(self, client):
        """Re-ticking a box after every typo is how people give up."""
        clinic_id, token = self._setup(
            client, "tick-owner@test.com", "tick-new@test.com")
        r = register(client, "tick-new@test.com", clinic_invite=token,
                     password="short", also_own_practice=True)
        assert r.status_code == 400
        assert re.search(r'id="also_own_practice"[^>]*checked', r.text) or \
               re.search(r'checked[^>]*id="also_own_practice"', r.text), (
            "the own-practice choice was silently un-ticked on retry")

    def test_own_practice_can_be_named(self, client):
        """Opting into a practice you cannot name is a poor first impression."""
        clinic_id, token = self._setup(
            client, "name-owner@test.com", "name-new@test.com")
        r = client.get(f"/register?clinic_invite={token}")
        assert 'id="ownPracticeNameGroup"' in r.text

        register(client, "name-new@test.com", clinic_invite=token,
                 also_own_practice=True, clinic_name="Sunrise Family Practice")
        db = TestSessionLocal()
        try:
            d = db.query(Doctor).filter(Doctor.email == "name-new@test.com").first()
            owned = db.query(Clinic).filter(Clinic.owner_doctor_id == d.id).first()
            assert owned is not None, "own practice was not created"
            assert owned.name == "Sunrise Family Practice", (
                f"practice name ignored: {owned.name!r}")
        finally:
            db.close()


# --------------------------------------------------------------------------- #
#  8. Telling the invitee the truth before they do the work                     #
# --------------------------------------------------------------------------- #

class TestInviteFormGuidesTheInvitee:

    def test_email_is_prefilled_from_the_invite(self, client):
        """A mismatch is rejected outright, so do not let them type one."""
        client.cookies.clear()
        _, clinic_id = make_clinic_owner(client, "pre-owner@test.com")
        token = make_invite(clinic_id, "pre-new@test.com")

        client.cookies.clear()
        r = client.get(f"/register?clinic_invite={token}")
        assert re.search(r'id="email"[^>]*value="pre-new@test\.com"', r.text, re.S), (
            "the invited address was not pre-filled")
        assert re.search(r'id="email"[^>]*readonly', r.text, re.S), (
            "the address is editable even though a mismatch is always refused")

    def test_dead_token_is_reported_before_the_form_is_filled(self, client):
        """Eight fields, then 'this invite expired', is how trust is lost."""
        client.cookies.clear()
        _, clinic_id = make_clinic_owner(client, "dead-owner@test.com")
        token = make_invite(clinic_id, "dead-new@test.com", days=-1)

        client.cookies.clear()
        r = client.get(f"/register?clinic_invite={token}")
        assert r.status_code == 200
        assert "no longer valid" in r.text, (
            "an expired invite looked like a normal signup page")

    def test_dead_token_does_not_ride_along_in_the_hidden_field(self, client):
        """Otherwise the POST refuses and they cannot sign up at all."""
        client.cookies.clear()
        _, clinic_id = make_clinic_owner(client, "dead2-owner@test.com")
        token = make_invite(clinic_id, "dead2-new@test.com", days=-1)

        client.cookies.clear()
        r = client.get(f"/register?clinic_invite={token}")
        assert not re.search(
            rf'name="clinic_invite"[^>]*value="{re.escape(token)}"', r.text), (
            "a dead token is still carried into the form, so the signup that "
            "the page invites them to complete is guaranteed to be refused")

    def test_they_can_still_sign_up_normally_after_a_dead_invite(self, client):
        client.cookies.clear()
        _, clinic_id = make_clinic_owner(client, "dead3-owner@test.com")
        token = make_invite(clinic_id, "dead3-new@test.com", days=-1)

        client.cookies.clear()
        client.get(f"/register?clinic_invite={token}")
        r = register(client, "dead3-new@test.com")     # no token, as the page renders it
        assert r.status_code in (200, 302, 303)
        assert doctor_row("dead3-new@test.com") is not None, (
            "a dead invite left them unable to create an account at all")
