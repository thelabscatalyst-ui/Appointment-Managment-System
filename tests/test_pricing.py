"""
test_pricing.py — the price list is one number in one place, and only what is
for sale can be bought.

Two failure modes this locks down:

  * Drift. Five templates each hardcoded "₹599", so changing a price meant
    finding it in five places and one was always missed — the same defect the
    asset-version cache-buster had.

  * Selling something that is not for sale. PLAN_AMOUNTS carries retired and
    legacy tiers so historical rows still resolve; if create_order accepted
    those keys, a hand-posted plan=duo would charge a price no page shows.
    Enterprise is worse: it is quoted per headcount, so its stored `amount` is
    only the 5-doctor floor — selling at that floor would hand a 30-doctor
    clinic unlimited seats for the price of five.

No live payment gateway is touched: conftest blanks the Razorpay keys for the
whole suite, so create_order short-circuits before any network call.
"""
import re
from datetime import datetime

import pytest

from tests.helpers import make_doctor, clinic_of, give_schedule
from services.payment_service import (PLAN_CONFIG, PLAN_AMOUNTS, enterprise_quote,
                                      price_display, create_order,
                                      ENTERPRISE_BASE_PAISE,
                                      ENTERPRISE_INCLUDED_SEATS,
                                      ENTERPRISE_PER_EXTRA_PAISE)


@pytest.fixture
def doc(client):
    client.cookies.clear()
    email = f"price-{datetime.utcnow().timestamp()}@test.com".replace(".", "-", 1)
    did = make_doctor(client, email)
    give_schedule(did, clinic_of(did))
    return {"id": did, "email": email}


class TestPriceList:

    def test_the_three_sellable_tiers(self):
        assert set(PLAN_CONFIG) == {"solo", "clinic", "enterprise"}

    def test_solo_is_999(self):
        assert PLAN_CONFIG["solo"]["amount"] == 99900
        assert PLAN_CONFIG["solo"]["seats"] == 1

    def test_clinic_is_1999_for_five(self):
        assert PLAN_CONFIG["clinic"]["amount"] == 199900
        assert PLAN_CONFIG["clinic"]["seats"] == 5

    def test_retired_tiers_still_resolve_for_old_rows(self):
        """plan_type is a Postgres enum; a historical 'duo' row must still load."""
        for legacy in ("duo", "hospital", "basic", "pro"):
            assert legacy in PLAN_AMOUNTS, f"{legacy} no longer resolves"
            assert legacy not in PLAN_CONFIG, f"{legacy} is still on sale"


class TestEnterpriseQuote:
    """₹1,999 base covering 5 doctors, then ₹300 each."""

    @pytest.mark.parametrize("doctors,rupees", [
        (5, 1999), (6, 2299), (8, 2899), (10, 3499), (15, 4999), (25, 7999),
    ])
    def test_quote(self, doctors, rupees):
        assert enterprise_quote(doctors)["rupees"] == rupees

    def test_no_cliff_at_the_clinic_boundary(self):
        """A clinic outgrowing 5 seats pays ₹300 for the sixth, not a new base."""
        clinic = PLAN_CONFIG["clinic"]["amount"]
        assert enterprise_quote(5)["amount"] == clinic
        assert enterprise_quote(6)["amount"] == clinic + ENTERPRISE_PER_EXTRA_PAISE

    def test_below_the_included_count_never_undercuts_clinic(self):
        for n in (0, 1, 3, 5):
            assert enterprise_quote(n)["amount"] == ENTERPRISE_BASE_PAISE

    def test_quote_is_linear(self):
        a, b = enterprise_quote(10)["amount"], enterprise_quote(11)["amount"]
        assert b - a == ENTERPRISE_PER_EXTRA_PAISE


class TestOnlySellableTiersCanBeBought:

    def test_enterprise_is_quote_only(self):
        r = create_order("enterprise")
        assert "error" in r
        assert "contact" in r["error"].lower(), r

    @pytest.mark.parametrize("retired", ["duo", "hospital", "basic", "pro"])
    def test_retired_tiers_are_refused(self, retired):
        assert "error" in create_order(retired)

    def test_unknown_plan_is_refused(self):
        assert "error" in create_order("free-forever")

    def test_sellable_tiers_reach_the_gateway_check(self, client, doc):
        """With keys blanked they stop at 'not configured' — never a 500, and
        never a real order."""
        for plan in ("solo", "clinic"):
            r = create_order(plan)
            assert "error" in r and "not configured" in r["error"].lower(), r

    def test_the_endpoint_refuses_a_hand_posted_retired_plan(self, client, doc):
        r = client.post("/billing/create-order", json={"plan": "duo"},
                        follow_redirects=False)
        assert r.status_code < 500
        body = r.text.lower()
        assert "order_id" not in body, "a retired tier produced an order"


class TestPricesAreSingleSourced:

    PUBLIC = ["/login", "/", "/pricing"]

    def test_no_template_hardcodes_a_rupee_price(self):
        """The drift bug: five <head>s, five copies of "₹599"."""
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent / "templates"
        offenders = []
        for f in root.rglob("*.html"):
            for line in f.read_text().splitlines():
                # Only the plan price list matters — bill amounts and demo
                # figures on the landing page are content, not the price list.
                # Headline prices AND per-doctor figures: the first pass of
                # this test only checked headlines, so a stale "₹320 per
                # doctor" survived on two pages.
                is_plan_price = re.search(
                    r"₹\s?(599|999|1,599|1,999|2,499|3,999)\b", line)
                is_per_doctor = ("per doctor" in line and re.search(r"₹\s?\d", line))
                if (is_plan_price or is_per_doctor) \
                        and "request.state.price" not in line:
                    offenders.append(f"{f.name}: {line.strip()[:70]}")
        assert offenders == [], "hardcoded plan prices: " + " | ".join(offenders)

    @pytest.mark.parametrize("path", PUBLIC)
    def test_public_pages_show_the_configured_price(self, client, path):
        client.cookies.clear()
        body = client.get(path, follow_redirects=True).text
        assert f"₹{price_display()['solo']}" in body, (
            f"{path} does not show the configured solo price")
        assert "₹599" not in body, f"{path} still advertises the old price"

    def test_pricing_page_shows_all_three_tiers(self, client):
        client.cookies.clear()
        body = client.get("/pricing").text
        p = price_display()
        assert f"₹{p['solo']}" in body
        assert f"₹{p['clinic']}" in body
        assert f"₹{p['ent_base']}" in body and f"₹{p['ent_extra']}" in body, (
            "the enterprise formula is not shown")

    def test_enterprise_still_says_contact_us(self, client):
        client.cookies.clear()
        body = client.get("/pricing").text
        assert "Contact us" in body, (
            "enterprise must stay a conversation, not a checkout button")
        assert "startPayment('enterprise'" not in body, (
            "enterprise has a self-serve buy button")

    def test_changing_the_config_moves_every_page(self, client, monkeypatch):
        """The whole point of single-sourcing."""
        monkeypatch.setitem(PLAN_CONFIG["solo"], "amount", 123400)
        client.cookies.clear()
        for path in ("/login", "/pricing"):
            assert "₹1,234" in client.get(path, follow_redirects=True).text, (
                f"{path} did not follow the price change")


class TestGatewayFailuresAreHandledWell:
    """Production ran with an invalid Razorpay key pair, and the first signal
    was a doctor unable to pay.

    Two defects that made it worse than it needed to be:
      * Razorpay's own wording ("Authentication failed") went straight to the
        doctor, who reasonably read it as THEIR login failing.
      * /billing/create-order returned 200 OK on failure, so nothing upstream
        could distinguish a broken gateway from a working one.
    """

    def _force(self, monkeypatch, exc_text):
        """Make the gateway raise the way a bad key pair does."""
        import services.payment_service as ps

        class _Boom:
            class order:
                @staticmethod
                def create(*a, **k):
                    raise Exception(exc_text)

        monkeypatch.setattr(ps, "_razorpay_client", lambda: _Boom())

    def test_auth_failure_does_not_blame_the_doctor(self, monkeypatch):
        from services.payment_service import create_order
        self._force(monkeypatch, "Authentication failed")
        r = create_order("solo")
        assert "Authentication failed" not in r["error"], (
            "the gateway's wording reached the doctor — it reads as though "
            "their own login failed")
        assert "temporarily unavailable" in r["error"].lower()
        assert r["reason"] == "gateway_auth"

    def test_the_doctor_is_told_nothing_was_charged(self, monkeypatch):
        """The first question anyone asks after a failed payment."""
        from services.payment_service import create_order
        for text in ("Authentication failed", "Gateway timeout"):
            self._force(monkeypatch, text)
            assert "charged" in create_order("solo")["error"].lower()

    def test_generic_failures_are_also_wrapped(self, monkeypatch):
        from services.payment_service import create_order
        self._force(monkeypatch, "connection reset by peer")
        r = create_order("clinic")
        assert "connection reset" not in r["error"]
        assert r["reason"] == "gateway_error"

    def test_endpoint_returns_502_for_a_broken_gateway(self, client, doc, monkeypatch):
        """200 OK on failure hid a dead payment gateway from every monitor."""
        self._force(monkeypatch, "Authentication failed")
        r = client.post("/billing/create-order?plan=solo", follow_redirects=False)
        assert r.status_code == 502, (
            f"a failed order returned {r.status_code}; a broken gateway must "
            f"not look like success")
        assert "error" in r.json()

    def test_endpoint_returns_400_for_a_bad_plan(self, client, doc):
        r = client.post("/billing/create-order?plan=duo", follow_redirects=False)
        assert r.status_code == 400
        assert "order_id" not in r.json()

    def test_success_still_returns_200(self, client, doc, monkeypatch):
        import services.payment_service as ps

        class _Ok:
            class order:
                @staticmethod
                def create(*a, **k):
                    return {"id": "order_test123", "amount": 99900, "currency": "INR"}

        monkeypatch.setattr(ps, "_razorpay_client", lambda: _Ok())
        r = client.post("/billing/create-order?plan=solo", follow_redirects=False)
        assert r.status_code == 200
        assert r.json()["order_id"] == "order_test123"


class TestLiveKeysAreBlockedOutsideProduction:
    """Live Razorpay keys live in .env for local development, so any code path
    reaching the gateway from a dev machine creates real orders on the real
    account. That happened three times while this file was being written.

    Orders are payment intents, so no money moved — but "no money moved" is
    luck, not a control. The control is refusing to talk to a live gateway from
    a non-production environment.
    """

    def _keys(self, monkeypatch, key_id, environment):
        from config import settings
        monkeypatch.setattr(settings, "RAZORPAY_KEY_ID", key_id)
        monkeypatch.setattr(settings, "RAZORPAY_KEY_SECRET", "secret")
        monkeypatch.setattr(settings, "ENVIRONMENT", environment)

    def test_live_keys_blocked_in_development(self, monkeypatch):
        from services.payment_service import live_keys_blocked
        monkeypatch.delenv("ALLOW_LIVE_PAYMENTS_OUTSIDE_PROD", raising=False)
        self._keys(monkeypatch, "rzp_live_abc123", "development")
        assert live_keys_blocked() is True

    def test_live_keys_allowed_in_production(self, monkeypatch):
        from services.payment_service import live_keys_blocked
        self._keys(monkeypatch, "rzp_live_abc123", "production")
        assert live_keys_blocked() is False

    def test_test_keys_are_never_blocked(self, monkeypatch):
        from services.payment_service import live_keys_blocked
        self._keys(monkeypatch, "rzp_test_abc123", "development")
        assert live_keys_blocked() is False

    def test_explicit_opt_in_unblocks(self, monkeypatch):
        """A deliberate end-to-end check must still be possible."""
        from services.payment_service import live_keys_blocked
        self._keys(monkeypatch, "rzp_live_abc123", "development")
        monkeypatch.setenv("ALLOW_LIVE_PAYMENTS_OUTSIDE_PROD", "1")
        assert live_keys_blocked() is False

    def test_blocked_keys_never_reach_the_gateway(self, monkeypatch):
        """The whole point: no client is built, so no HTTP call happens."""
        import services.payment_service as ps

        monkeypatch.delenv("ALLOW_LIVE_PAYMENTS_OUTSIDE_PROD", raising=False)
        self._keys(monkeypatch, "rzp_live_abc123", "development")

        called = []
        monkeypatch.setattr("razorpay.Client",
                            lambda *a, **k: called.append(1) or object())
        r = ps.create_order("solo")
        assert called == [], "a live gateway client was constructed anyway"
        assert r["reason"] == "live_keys_blocked"

    def test_the_message_does_not_claim_keys_are_missing(self, monkeypatch):
        """Saying "not configured" for present-but-blocked keys sent someone
        hunting for keys that were there all along."""
        import services.payment_service as ps
        monkeypatch.delenv("ALLOW_LIVE_PAYMENTS_OUTSIDE_PROD", raising=False)
        self._keys(monkeypatch, "rzp_live_abc123", "development")
        assert "not configured" not in ps.create_order("solo")["error"].lower()


class TestPlanCannotBeEscalatedAtVerify:
    """The checkout signature is an HMAC over "order_id|payment_id" only.

    It proves a real payment happened against that order. It says nothing about
    which plan or how much — so /billing/verify must read the plan back from
    the order Razorpay actually charged, never from the form.

    Taking it from the form meant a doctor could pay ₹999 for Solo and post
    plan=enterprise to be granted unlimited doctor seats. Retiring Duo and
    Hospital from PLAN_CONFIG made it worse: PLAN_CONFIG.get(plan, {}).get(
    "seats") returned None for them, and None reads as unlimited downstream.
    """

    def test_seats_never_default_to_unlimited(self):
        """The specific mechanism that turned a retired tier into unlimited."""
        from services.payment_service import seats_for_plan
        assert seats_for_plan("solo") == 1
        assert seats_for_plan("clinic") == 5
        assert seats_for_plan("enterprise") is None      # genuinely unlimited
        for retired in ("hospital", "duo", "basic", "pro", "bogus"):
            with pytest.raises(KeyError):
                seats_for_plan(retired)

    def _order(self, monkeypatch, notes_plan, amount):
        import services.payment_service as ps

        class _Client:
            class order:
                @staticmethod
                def fetch(order_id):
                    return {"id": order_id, "amount": amount,
                            "notes": {"plan": notes_plan, "product": "Med Track"}}

        monkeypatch.setattr(ps, "_razorpay_client", lambda: _Client())

    def test_plan_comes_from_the_order(self, monkeypatch):
        from services.payment_service import verified_plan_for_order, PLAN_CONFIG
        self._order(monkeypatch, "solo", PLAN_CONFIG["solo"]["amount"])
        plan, reason = verified_plan_for_order("order_x")
        assert (plan, reason) == ("solo", "ok")

    def test_amount_must_match_the_plan(self, monkeypatch):
        """Matching notes with the wrong amount means it is not our order."""
        from services.payment_service import verified_plan_for_order
        self._order(monkeypatch, "enterprise", 99900)   # enterprise at solo's price
        plan, reason = verified_plan_for_order("order_x")
        assert plan is None and reason == "amount_mismatch"

    def test_a_retired_tier_in_the_order_is_refused(self, monkeypatch):
        from services.payment_service import verified_plan_for_order
        self._order(monkeypatch, "hospital", 249900)
        plan, reason = verified_plan_for_order("order_x")
        assert plan is None and reason == "unknown_plan"

    def test_unreachable_gateway_fails_closed(self, monkeypatch):
        """Never grant a tier we could not confirm."""
        import services.payment_service as ps
        from services.payment_service import verified_plan_for_order

        class _Client:
            class order:
                @staticmethod
                def fetch(order_id):
                    raise Exception("network down")

        monkeypatch.setattr(ps, "_razorpay_client", lambda: _Client())
        plan, reason = verified_plan_for_order("order_x")
        assert plan is None and reason == "fetch_failed"

    def test_verify_endpoint_ignores_a_forged_plan_field(self, client, doc, monkeypatch):
        """The exploit, end to end: pay for Solo, claim Enterprise."""
        import services.payment_service as ps
        from services.payment_service import PLAN_CONFIG
        from tests.conftest import TestSessionLocal
        from database.models import Doctor

        self._order(monkeypatch, "solo", PLAN_CONFIG["solo"]["amount"])
        # billing_verify imports verify_signature inside the function body, so
        # patching the module is enough — no route-level patch needed.
        monkeypatch.setattr(ps, "verify_signature", lambda *a, **k: True)

        r = client.post("/billing/verify", data={
            "razorpay_payment_id": "pay_x",
            "razorpay_order_id": "order_x",
            "razorpay_signature": "sig",
            "plan": "enterprise",          # <- the forgery
        }, follow_redirects=False)
        assert r.status_code in (302, 303)

        db = TestSessionLocal()
        try:
            d = db.query(Doctor).filter(Doctor.id == doc["id"]).first()
            assert d.plan_seats == 1, (
                f"paid for Solo but was granted {d.plan_seats} seats — the "
                f"forged plan field was honoured")
            assert d.plan_type.value == "solo", d.plan_type
        finally:
            db.close()


class TestPendingActivationIsVisible:
    def test_pending_tells_the_doctor_what_happened(self, client, doc):
        """A payment that could not be confirmed used to redirect to a page
        that rendered nothing at all — paid, and silence."""
        # Collapse whitespace: the sentence wraps across lines in the
        # template, so a raw substring check fails on the newline.
        body = " ".join(client.get("/billing?success=pending").text.lower().split())
        assert "activation pending" in body
        assert "nothing further is owed" in body
        assert "could not confirm which plan" in body
