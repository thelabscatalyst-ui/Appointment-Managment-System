"""
Payment service — Razorpay order creation and signature verification.

Plans are seat-based: same features for everyone, price scales with doctor count.
"""
import hmac
import hashlib
import logging

from config import settings

logger = logging.getLogger(__name__)

# Single source of truth for all plan metadata
PLAN_CONFIG = {
    "solo": {
        "amount":    59900,   # paise  → ₹599
        "seats":     1,
        "label":     "Solo",
        "per_doctor": 599,
    },
    "duo": {
        "amount":    69900,   # paise  → ₹699
        "seats":     2,
        "label":     "Duo",
        "per_doctor": 350,
    },
    "clinic": {
        "amount":    159900,  # paise  → ₹1,599
        "seats":     5,
        "label":     "Clinic",
        "per_doctor": 320,
    },
    "hospital": {
        "amount":    249900,  # paise  → ₹2,499
        "seats":     15,
        "label":     "Hospital",
        "per_doctor": 167,
    },
    "enterprise": {
        "amount":    399900,  # paise  → ₹3,999
        "seats":     None,    # unlimited
        "label":     "Enterprise",
        "per_doctor": None,
    },
}

# Keep legacy amounts accessible for old subscription records
PLAN_AMOUNTS = {k: v["amount"] for k, v in PLAN_CONFIG.items()}
# legacy plans
PLAN_AMOUNTS["basic"] = 29900
PLAN_AMOUNTS["pro"]   = 49900


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
    if plan not in PLAN_AMOUNTS:
        return {"error": f"Unknown plan: {plan}"}

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
        logger.error(f"Razorpay order creation failed: {exc}")
        return {"error": str(exc)}


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
