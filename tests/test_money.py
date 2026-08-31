"""
test_money.py — billing, the price catalog, expenses and the income reports.

Money is where a silent bug is most expensive, and where the clinic-scoping
rules are strictest: a doctor who works at two clinics must never see one
clinic's takings blended into the other's, and an associate must not see the
host clinic's finances at all.

Every figure asserted here is read back from the database, not from the page,
except where the page total is the thing under test.
"""
from datetime import datetime, date, timedelta

import pytest

from tests.conftest import TestSessionLocal
from tests.helpers import (make_doctor, clinic_of, make_patient, make_visit,
                           give_schedule, set_pin, login, phone, register,
                           verify_email)
from database.models import (Bill, BillItem, PriceCatalog, Expense,
                             RecurringExpense, Visit, VisitStatus, PaymentMode)


@pytest.fixture
def doc(client):
    client.cookies.clear()
    email = f"money-{datetime.utcnow().timestamp()}@test.com".replace(".", "-", 1)
    did = make_doctor(client, email)
    cid = clinic_of(did)
    give_schedule(did, cid)
    pid = make_patient(did, cid, name="Paying Patient")
    set_pin(client)
    return {"id": did, "clinic": cid, "patient": pid, "email": email}


def bills_of(doctor_id):
    db = TestSessionLocal()
    try:
        return db.query(Bill).filter(Bill.doctor_id == doctor_id).all()
    finally:
        db.close()


# --------------------------------------------------------------------------- #
#  Price catalog                                                                #
# --------------------------------------------------------------------------- #

class TestPriceCatalog:

    def test_list_is_json(self, client, doc):
        r = client.get("/price-catalog")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")

    def test_add_item(self, client, doc):
        r = client.post("/price-catalog",
                        data={"name": "Consultation", "price": "500", "pinned": "1"},
                        follow_redirects=False)
        assert r.status_code in (200, 302, 303)
        db = TestSessionLocal()
        try:
            item = db.query(PriceCatalog).filter(
                PriceCatalog.doctor_id == doc["id"]).first()
            assert item is not None and float(item.default_price) == 500.0
        finally:
            db.close()

    def test_pin_and_delete_item(self, client, doc):
        client.post("/price-catalog", data={"name": "X-Ray", "price": "800"},
                    follow_redirects=False)
        db = TestSessionLocal()
        try:
            iid = db.query(PriceCatalog).filter(
                PriceCatalog.doctor_id == doc["id"]).first().id
        finally:
            db.close()

        assert client.post(f"/price-catalog/{iid}/pin",
                           follow_redirects=False).status_code in (200, 302, 303)
        client.post(f"/price-catalog/{iid}/delete", follow_redirects=False)

        db = TestSessionLocal()
        try:
            row = db.query(PriceCatalog).filter(PriceCatalog.id == iid).first()
            assert row is None or row.is_active is False, "item was not removed"
        finally:
            db.close()

    def test_another_doctor_cannot_delete_your_prices(self, client, doc):
        client.post("/price-catalog", data={"name": "Dressing", "price": "300"},
                    follow_redirects=False)
        db = TestSessionLocal()
        try:
            iid = db.query(PriceCatalog).filter(
                PriceCatalog.doctor_id == doc["id"]).first().id
        finally:
            db.close()

        make_doctor(client, "money-price-intruder@test.com")
        set_pin(client)
        client.post(f"/price-catalog/{iid}/delete", follow_redirects=False)
        db = TestSessionLocal()
        try:
            row = db.query(PriceCatalog).filter(PriceCatalog.id == iid).first()
            assert row is not None and row.is_active is not False, (
                "another doctor deleted your price catalog item")
        finally:
            db.close()


# --------------------------------------------------------------------------- #
#  Billing a visit                                                              #
# --------------------------------------------------------------------------- #

class TestBillAVisit:

    def _visit(self, doc):
        return make_visit(doc["id"], doc["patient"], doc["clinic"],
                          status=VisitStatus.billing_pending)

    def test_prefill_is_json(self, client, doc):
        v = self._visit(doc)
        r = client.get(f"/visits/{v}/bill-prefill")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")

    def test_bill_with_line_items_totals_correctly(self, client, doc):
        v = self._visit(doc)
        r = client.post(f"/visits/{v}/bill", data={
            "action": "close", "fee": "0", "discount": "0", "gst_amount": "0",
            "payment_mode": "cash", "notes": "",
            "item_name": ["Consultation", "Dressing"],
            "item_price": ["500", "300"],
            "item_qty": ["1", "2"],
        }, follow_redirects=False)
        assert r.status_code in (200, 302, 303), r.text[:300]

        all_bills = bills_of(doc["id"])
        assert len(all_bills) == 1, f"expected one bill, got {len(all_bills)}"
        # 500*1 + 300*2 = 1100
        assert float(all_bills[0].total) == 1100.0, (
            f"line items did not total correctly: {all_bills[0].total}")

    def test_discount_reduces_the_total(self, client, doc):
        v = self._visit(doc)
        client.post(f"/visits/{v}/bill", data={
            "action": "close", "fee": "1000", "discount": "200",
            "gst_amount": "0", "payment_mode": "cash", "notes": "",
            "item_name": [], "item_price": [], "item_qty": [],
        }, follow_redirects=False)
        total = float(bills_of(doc["id"])[0].total)
        assert total == 800.0, f"discount not applied: {total}"

    def test_blank_line_items_are_ignored(self, client, doc):
        v = self._visit(doc)
        client.post(f"/visits/{v}/bill", data={
            "action": "close", "fee": "500", "discount": "0", "gst_amount": "0",
            "payment_mode": "cash", "notes": "",
            "item_name": ["", "  "], "item_price": ["100", "200"],
            "item_qty": ["1", "1"],
        }, follow_redirects=False)
        total = float(bills_of(doc["id"])[0].total)
        assert total == 500.0, (
            f"unnamed line items were charged to the patient: {total}")

    def test_bill_closes_the_visit(self, client, doc):
        v = self._visit(doc)
        client.post(f"/visits/{v}/bill", data={
            "action": "close", "fee": "400", "discount": "0", "gst_amount": "0",
            "payment_mode": "cash", "notes": "",
            "item_name": [], "item_price": [], "item_qty": [],
        }, follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert db.query(Visit).filter(Visit.id == v).first().status \
                == VisitStatus.done
        finally:
            db.close()

    def test_bill_is_stamped_with_the_clinic(self, client, doc):
        v = self._visit(doc)
        client.post(f"/visits/{v}/bill", data={
            "action": "close", "fee": "400", "discount": "0", "gst_amount": "0",
            "payment_mode": "cash", "notes": "",
            "item_name": [], "item_price": [], "item_qty": [],
        }, follow_redirects=False)
        assert bills_of(doc["id"])[0].clinic_id == doc["clinic"], (
            "an unstamped bill is invisible to every clinic-scoped income query")

    def test_another_doctor_cannot_bill_your_visit(self, client, doc):
        v = self._visit(doc)
        intruder = make_doctor(client, "money-bill-intruder@test.com")
        set_pin(client)
        client.post(f"/visits/{v}/bill", data={
            "action": "close", "fee": "9999", "discount": "0", "gst_amount": "0",
            "payment_mode": "cash", "notes": "",
            "item_name": [], "item_price": [], "item_qty": [],
        }, follow_redirects=False)
        assert bills_of(intruder) == [], "a doctor billed another doctor's visit"


class TestBillReadEditPdf:

    def _billed(self, client, doc):
        v = make_visit(doc["id"], doc["patient"], doc["clinic"],
                       status=VisitStatus.billing_pending)
        client.post(f"/visits/{v}/bill", data={
            "action": "close", "fee": "600", "discount": "0", "gst_amount": "0",
            "payment_mode": "cash", "notes": "",
            "item_name": [], "item_price": [], "item_qty": [],
        }, follow_redirects=False)
        return bills_of(doc["id"])[0].id

    def test_detail_and_edit_render(self, client, doc):
        bid = self._billed(client, doc)
        assert client.get(f"/bills/{bid}").status_code == 200
        assert client.get(f"/bills/{bid}/edit").status_code == 200

    def test_pdf_is_generated(self, client, doc):
        bid = self._billed(client, doc)
        r = client.get(f"/bills/{bid}/pdf")
        assert r.status_code == 200, r.text[:200]
        assert r.content[:4] == b"%PDF", "the bill PDF is not a PDF"

    def test_edit_changes_the_total(self, client, doc):
        bid = self._billed(client, doc)
        # /bills/{id}/edit recomputes from line items — it has no `fee` field,
        # and with no items it deliberately keeps the existing subtotal.
        client.post(f"/bills/{bid}/edit", data={
            "discount": "0", "gst_amount": "0",
            "payment_mode": "cash", "notes": "corrected",
            "item_name": ["Revised consultation"], "item_price": ["750"],
            "item_qty": ["1"],
        }, follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert float(db.query(Bill).filter(Bill.id == bid).first().total) == 750.0
        finally:
            db.close()

    def test_mark_paid(self, client, doc):
        bid = self._billed(client, doc)
        r = client.post(f"/bills/{bid}/mark-paid", follow_redirects=False)
        assert r.status_code in (200, 302, 303)
        db = TestSessionLocal()
        try:
            assert db.query(Bill).filter(Bill.id == bid).first().paid_at is not None
        finally:
            db.close()

    def test_another_doctor_cannot_read_or_edit_a_bill(self, client, doc):
        bid = self._billed(client, doc)
        make_doctor(client, "money-bill-reader@test.com")
        set_pin(client)
        for path in (f"/bills/{bid}", f"/bills/{bid}/edit", f"/bills/{bid}/pdf"):
            r = client.get(path, follow_redirects=False)
            assert r.status_code != 200, f"{path} leaked another doctor's bill"

        client.post(f"/bills/{bid}/edit", data={
            "fee": "1", "discount": "0", "gst_amount": "0",
            "payment_mode": "cash", "notes": "",
            "item_name": [], "item_price": [], "item_qty": [],
        }, follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert float(db.query(Bill).filter(Bill.id == bid).first().total) == 600.0, (
                "another doctor rewrote this bill")
        finally:
            db.close()

    def test_unknown_bill_is_not_a_500(self, client, doc):
        for path in ("/bills/999999", "/bills/999999/edit", "/bills/999999/pdf"):
            assert client.get(path, follow_redirects=False).status_code < 500


# --------------------------------------------------------------------------- #
#  Expenses                                                                     #
# --------------------------------------------------------------------------- #

class TestExpenses:

    def test_page_renders(self, client, doc):
        assert client.get("/expenses").status_code == 200

    def test_add_and_delete(self, client, doc):
        r = client.post("/expenses", data={
            "amount": "1500", "category": "rent", "note": "March rent",
            "spent_on": date.today().isoformat(),
        }, follow_redirects=False)
        assert r.status_code in (200, 302, 303)

        db = TestSessionLocal()
        try:
            e = db.query(Expense).filter(Expense.doctor_id == doc["id"]).first()
            assert e is not None and float(e.amount) == 1500.0
            eid = e.id
            assert e.clinic_id == doc["clinic"], "expense not stamped with the clinic"
        finally:
            db.close()

        client.post(f"/expenses/{eid}/delete", follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert db.query(Expense).filter(Expense.id == eid).first() is None
        finally:
            db.close()

    def test_bad_amount_does_not_500(self, client, doc):
        r = client.post("/expenses", data={
            "amount": "not-a-number", "category": "misc", "note": "",
            "spent_on": date.today().isoformat(),
        }, follow_redirects=False)
        assert r.status_code < 500

    def test_unknown_category_does_not_500(self, client, doc):
        r = client.post("/expenses", data={
            "amount": "100", "category": "not-a-category", "note": "",
            "spent_on": date.today().isoformat(),
        }, follow_redirects=False)
        assert r.status_code < 500

    def test_recurring_rule_lifecycle(self, client, doc):
        r = client.post("/expenses/recurring", data={
            "amount": "20000", "category": "salaries", "label": "Nurse salary",
            "day_of_month": "1",
        }, follow_redirects=False)
        assert r.status_code in (200, 302, 303)

        db = TestSessionLocal()
        try:
            rule = db.query(RecurringExpense).filter(
                RecurringExpense.doctor_id == doc["id"]).first()
            assert rule is not None
            rid, was_active = rule.id, rule.is_active
        finally:
            db.close()

        client.post(f"/expenses/recurring/{rid}/toggle", follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert db.query(RecurringExpense).filter(
                RecurringExpense.id == rid).first().is_active != was_active
        finally:
            db.close()

        client.post(f"/expenses/recurring/{rid}/delete", follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert db.query(RecurringExpense).filter(
                RecurringExpense.id == rid).first() is None
        finally:
            db.close()

    def test_day_of_month_is_clamped(self, client, doc):
        """31 would silently skip short months; the route clamps to 28."""
        client.post("/expenses/recurring", data={
            "amount": "100", "category": "misc", "label": "Edge",
            "day_of_month": "31",
        }, follow_redirects=False)
        db = TestSessionLocal()
        try:
            rule = db.query(RecurringExpense).filter(
                RecurringExpense.doctor_id == doc["id"]).first()
            assert 1 <= rule.day_of_month <= 28, rule.day_of_month
        finally:
            db.close()

    def test_another_doctor_cannot_delete_your_expense(self, client, doc):
        client.post("/expenses", data={
            "amount": "999", "category": "misc", "note": "mine",
            "spent_on": date.today().isoformat()}, follow_redirects=False)
        db = TestSessionLocal()
        try:
            eid = db.query(Expense).filter(Expense.doctor_id == doc["id"]).first().id
        finally:
            db.close()

        make_doctor(client, "money-exp-intruder@test.com")
        set_pin(client)
        client.post(f"/expenses/{eid}/delete", follow_redirects=False)
        db = TestSessionLocal()
        try:
            assert db.query(Expense).filter(Expense.id == eid).first() is not None, (
                "another doctor deleted your expense")
        finally:
            db.close()


# --------------------------------------------------------------------------- #
#  Income reporting                                                             #
# --------------------------------------------------------------------------- #

class TestIncomeReports:

    def test_income_and_transactions_render(self, client, doc):
        assert client.get("/income").status_code == 200
        assert client.get("/income/transactions").status_code == 200

    def test_transactions_accepts_period_filters(self, client, doc):
        today = date.today()
        for qs in (f"?year={today.year}&view=yearly",
                   f"?year={today.year}&month={today.month}&view=monthly"):
            assert client.get(f"/income/transactions{qs}").status_code == 200

    def test_income_counts_a_paid_bill(self, client, doc):
        v = make_visit(doc["id"], doc["patient"], doc["clinic"],
                       status=VisitStatus.billing_pending)
        client.post(f"/visits/{v}/bill", data={
            "action": "close", "fee": "1234", "discount": "0", "gst_amount": "0",
            "payment_mode": "cash", "notes": "",
            "item_name": [], "item_price": [], "item_qty": [],
        }, follow_redirects=False)
        assert "1,234" in client.get("/income").text or "1234" in client.get("/income").text

    def test_reports_page_renders(self, client, doc):
        assert client.get("/reports").status_code == 200
