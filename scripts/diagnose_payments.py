"""
diagnose_payments.py — are the Razorpay keys in THIS environment actually valid?

Written after production spent an unknown stretch of time with a key pair that
failed authentication. Nothing detected it: the app only talks to Razorpay when
a doctor clicks Pay, so the first signal was a customer unable to buy.

Authenticates with a READ-ONLY call (GET /v1/orders?count=1). It creates no
order, takes no payment, and changes nothing — safe to run against live keys.

    python scripts/diagnose_payments.py                 # this machine's .env
    railway run python scripts/diagnose_payments.py     # production's env

Exit code 0 = usable, 1 = not.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings                                  # noqa: E402
from services.payment_service import (PLAN_CONFIG, price_display,   # noqa: E402
                                      enterprise_quote)


def _mask(v: str) -> str:
    if not v:
        return "NOT SET"
    if len(v) <= 12:
        return f"{v[:4]}… ({len(v)} chars)"
    return f"{v[:12]}… ({len(v)} chars)"


def main() -> int:
    kid = settings.RAZORPAY_KEY_ID or ""
    sec = settings.RAZORPAY_KEY_SECRET or ""

    print("\n=== Credentials ===")
    print(f"  RAZORPAY_KEY_ID     : {_mask(kid)}")
    print(f"  RAZORPAY_KEY_SECRET : {'set (' + str(len(sec)) + ' chars)' if sec else 'NOT SET'}")
    mode = "live" if kid.startswith("rzp_live") else "test" if kid.startswith("rzp_test") else "unknown"
    print(f"  mode                : {mode}")
    print(f"  ENVIRONMENT         : {settings.ENVIRONMENT}")

    problems = []
    if not kid or not sec:
        problems.append("keys are missing — checkout will say 'not configured'")
    # Invisible damage is a real cause: a value pasted with a trailing newline
    # looks identical in a dashboard and fails every request.
    for label, val in (("KEY_ID", kid), ("KEY_SECRET", sec)):
        if val and val != val.strip():
            problems.append(f"{label} has leading/trailing whitespace")
    if mode == "unknown" and kid:
        problems.append("KEY_ID does not look like rzp_test_… or rzp_live_…")
    if mode == "live" and settings.ENVIRONMENT.lower() != "production":
        problems.append("LIVE keys in a non-production environment — real "
                        "orders can be created from a dev machine")

    for p in problems:
        print(f"  [WARN] {p}")

    print("\n=== Price list ===")
    for key, cfg in PLAN_CONFIG.items():
        seats = "unlimited" if cfg["seats"] is None else cfg["seats"]
        sale = "quote only" if cfg.get("contact_only") else "self-serve"
        print(f"  {cfg['label']:11} ₹{cfg['amount'] // 100:>6,}  seats={seats:<9} {sale}")
    q = enterprise_quote(10)
    print(f"  (Enterprise at 10 doctors → ₹{q['rupees']:,})")

    if not kid or not sec:
        print("\nRESULT: payments are DISABLED in this environment.\n")
        return 1

    print("\n=== Live authentication check (read-only) ===")
    try:
        import requests
    except ImportError:
        print("  requests not installed — cannot verify")
        return 1

    try:
        r = requests.get("https://api.razorpay.com/v1/orders?count=1",
                         auth=(kid.strip(), sec.strip()), timeout=20)
    except Exception as exc:
        print(f"  could not reach Razorpay: {type(exc).__name__}: {exc}")
        return 1

    if r.status_code == 200:
        print("  HTTP 200 — keys are VALID. Nothing was created.")
        print("\nRESULT: payments are working in this environment.\n")
        return 0

    print(f"  HTTP {r.status_code} — {r.text[:160]}")
    if r.status_code == 401:
        print("\n  The key ID and secret are not a valid pair for this account.")
        print("  Usually one of:")
        print("    * the keys were regenerated in Razorpay and these are the old ones")
        print("    * the ID is from one key pair and the secret from another")
        print("    * a test key is paired with a live secret, or vice versa")
        print("  Fix: Razorpay Dashboard → Account & Settings → API Keys,")
        print("  generate a pair, and set BOTH values together.")
    print("\nRESULT: payments are BROKEN in this environment.\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
