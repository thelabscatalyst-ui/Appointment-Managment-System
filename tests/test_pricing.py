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
