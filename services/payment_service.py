"""
Payment service — Razorpay order creation and signature verification.

Plans are seat-based: same features for everyone, price scales with doctor count.
"""
import hmac
import hashlib
import logging

from config import settings

logger = logging.getLogger(__name__)

# Single source of truth for all plan metadata.
#
# Three sellable tiers. Duo (2 seats) and Hospital (15 seats) were removed from
# the catalogue — they were never shown on the pricing page, so nobody could
# buy them, but they sat here looking authoritative. Their PlanType enum
# members and PLAN_AMOUNTS entries are deliberately KEPT below: plan_type is a
# real Postgres enum, so a value cannot be dropped without recreating the type,
# and any historical row holding "duo" must still load.
PLAN_CONFIG = {
    "solo": {
        "amount":     99900,   # paise → ₹999
        "seats":      1,
        "label":      "Solo",
        "per_doctor": 999,
    },
    "clinic": {
        "amount":     199900,  # paise → ₹1,999
        "seats":      5,
        "label":      "Clinic",
        "per_doctor": 400,     # ₹1,999 / 5 seats
    },
    "enterprise": {
        # Quoted, not sold self-serve: ₹1,999 base + ₹300 per doctor beyond the
        # 5 included. `amount` is the floor (a 5-doctor quote) and exists only
        # so legacy records and the seat-rank logic still resolve — the pricing
        # page shows "Contact us", and create_order refuses this tier outright.
        "amount":     199900,
        "seats":      None,    # unlimited
        "label":      "Enterprise",
        "per_doctor": 300,
        "contact_only": True,
    },
}

# The included-seat count and the marginal price above it, in one place so the
# page, the quote helper and any future invoice all agree.
ENTERPRISE_BASE_PAISE     = 199900   # ₹1,999
ENTERPRISE_INCLUDED_SEATS = 5
ENTERPRISE_PER_EXTRA_PAISE = 30000   # ₹300 per doctor beyond the included 5


def enterprise_quote(doctors: int) -> dict:
    """Indicative Enterprise price for a given headcount.

    ₹1,999 covers the first 5 doctors, then ₹300 each. Deliberately continuous
    with the Clinic tier: a clinic outgrowing 5 seats pays ₹300 for the sixth
    rather than jumping to a new base, so there is no cliff at the boundary.
    """
    n = max(ENTERPRISE_INCLUDED_SEATS, int(doctors or 0))
    extra = n - ENTERPRISE_INCLUDED_SEATS
    paise = ENTERPRISE_BASE_PAISE + extra * ENTERPRISE_PER_EXTRA_PAISE
    return {
        "doctors": n,
        "extra_doctors": extra,
        "amount": paise,
        "rupees": paise // 100,
    }


# Legacy amounts, kept so historical subscription rows still resolve. Nothing
# here is purchasable — create_order only accepts keys in PLAN_CONFIG.
PLAN_AMOUNTS = {k: v["amount"] for k, v in PLAN_CONFIG.items()}
PLAN_AMOUNTS["duo"]      = 69900    # retired tier
PLAN_AMOUNTS["hospital"] = 249900   # retired tier
PLAN_AMOUNTS["basic"]    = 29900
PLAN_AMOUNTS["pro"]      = 49900


def _razorpay_client():
    if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
        return None
    try:
        import razorpay
        return razorpay.Client(
            auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
        )
    except Exception as exc:
        logger.error(f"Razorpay client init failed: {exc}")
        return None


def create_order(plan: str) -> dict:
    """Create a Razorpay order — always full price.

    Upgrades pay full price of the new plan; the remaining days from
    the current plan carry over (30 days are added to the existing expiry).
    """
    # PLAN_AMOUNTS also carries retired and legacy tiers so old rows resolve.
    # Only what is in PLAN_CONFIG is actually for sale — otherwise a hand-posted
    # plan=duo would charge a price no page advertises.
    if plan not in PLAN_CONFIG:
        return {"error": f"Unknown plan: {plan}"}

    # Enterprise is quoted, not self-serve: its price depends on headcount
    # (₹1,999 + ₹300 per doctor beyond 5), so the stored `amount` is only the
    # 5-doctor floor. Selling at that floor would hand a 30-doctor clinic
    # unlimited seats for the price of five.
    if PLAN_CONFIG[plan].get("contact_only"):
        return {"error": "Enterprise is priced per clinic — please contact us for a quote."}

    client = _razorpay_client()
    if not client:
        return {"error": "Payment gateway not configured. Add Razorpay keys to .env"}

    try:
        order = client.order.create({
            "amount":   PLAN_AMOUNTS[plan],
            "currency": "INR",
            "notes":    {"plan": plan, "product": "Med Track"},
        })
        return {
            "order_id": order["id"],
            "amount":   order["amount"],
            "currency": order["currency"],
            "key_id":   settings.RAZORPAY_KEY_ID,
            "plan":     plan,
        }
    except Exception as exc:
        # Never hand the gateway's own wording to a doctor. Razorpay says
        # "Authentication failed" when OUR API keys are wrong, which reads to a
        # customer as though THEIR login failed — that is exactly how the first
        # real report of this arrived. The operator needs the true reason; the
        # doctor needs to know it is not their fault and not their problem to
        # solve.
        detail = str(exc)
        logger.error(
            "Razorpay order creation failed (plan=%s): %s%s",
            plan, detail,
            "  <-- RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET are wrong for this "
            "environment; check them with scripts/diagnose_payments.py"
            if "authentication" in detail.lower() else "",
        )
        if "authentication" in detail.lower():
            return {
                "error": ("Payments are temporarily unavailable. Nothing was "
                          "charged — please contact us and we'll sort it out."),
                "reason": "gateway_auth",   # for logs/monitoring, not the doctor
            }
        return {"error": ("Could not start the payment. Nothing was charged — "
                          "please try again in a moment."),
                "reason": "gateway_error"}


def verify_signature(payment_id: str, order_id: str, signature: str) -> bool:
    if not settings.RAZORPAY_KEY_SECRET:
        return False
    try:
        msg      = f"{order_id}|{payment_id}".encode()
        expected = hmac.new(
            settings.RAZORPAY_KEY_SECRET.encode(),
            msg,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)
    except Exception as exc:
        logger.error(f"Signature verification error: {exc}")
        return False


def price_display() -> dict:
    """Formatted prices for templates.

    Five templates hardcoded "₹599" in their own markup, so a price change had
    to be found in five places and one was always missed. Everything now reads
    from PLAN_CONFIG through request.state.price.
    """
    def rs(paise):
        return f"{paise // 100:,}"

    return {
        "solo":              rs(PLAN_CONFIG["solo"]["amount"]),
        "clinic":            rs(PLAN_CONFIG["clinic"]["amount"]),
        "entry":             rs(min(PLAN_CONFIG[k]["amount"] for k in ("solo", "clinic"))),
        "clinic_per_doctor": f"{PLAN_CONFIG['clinic']['per_doctor']:,}",
        "solo_per_doctor":   f"{PLAN_CONFIG['solo']['per_doctor']:,}",
        "ent_base":          rs(ENTERPRISE_BASE_PAISE),
        "ent_extra":         rs(ENTERPRISE_PER_EXTRA_PAISE),
        "ent_included":      ENTERPRISE_INCLUDED_SEATS,
        "clinic_seats":      PLAN_CONFIG["clinic"]["seats"],
    }
