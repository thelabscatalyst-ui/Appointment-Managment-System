#!/usr/bin/env bash
#
# set_razorpay_keys.sh — copy the working Razorpay keys from .env into Railway.
#
# Run this yourself; it is the one step that has to be done by a human, because
# it puts a live payment secret into production configuration.
#
# It does everything around that step for you:
#   1. reads both keys from .env and strips any stray newline or carriage
#      return — piping a key straight from grep sends 25 bytes for a
#      24-character secret, and that invisible trailing newline gets stored,
#      looks identical in the dashboard, and fails every request
#   2. checks the keys actually authenticate BEFORE pushing them, so a bad
#      pair is never promoted to production
#   3. sets both variables on the service
#   4. waits for the redeploy and verifies production end to end
#
# Usage:  bash scripts/set_razorpay_keys.sh
#
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

SERVICE="Clincos"
PY="./venv/bin/python"

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }

bold "1/4  Reading keys from .env"

KEY_ID="$(grep '^RAZORPAY_KEY_ID='     .env 2>/dev/null | cut -d= -f2- | tr -d '\r\n')"
KEY_SECRET="$(grep '^RAZORPAY_KEY_SECRET=' .env 2>/dev/null | cut -d= -f2- | tr -d '\r\n')"

if [ -z "$KEY_ID" ] || [ -z "$KEY_SECRET" ]; then
  red "     RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET not found in .env — nothing to copy."
  exit 1
fi
echo "     id     : ${KEY_ID:0:12}… (${#KEY_ID} chars)"
echo "     secret : set (${#KEY_SECRET} chars)"

bold "2/4  Checking these keys authenticate (read-only, creates nothing)"

AUTH_OK="$(KEY_ID="$KEY_ID" KEY_SECRET="$KEY_SECRET" "$PY" - <<'PYEOF'
import os, requests
try:
    r = requests.get("https://api.razorpay.com/v1/orders?count=1",
                     auth=(os.environ["KEY_ID"], os.environ["KEY_SECRET"]), timeout=20)
    print("yes" if r.status_code == 200 else f"no:{r.status_code}")
except Exception as exc:
    print(f"no:{type(exc).__name__}")
PYEOF
)"

if [ "$AUTH_OK" != "yes" ]; then
  red "     These keys do NOT authenticate ($AUTH_OK)."
  red "     Refusing to push a broken pair to production — that is the bug you"
  red "     already have. Generate a fresh pair in the Razorpay dashboard"
  red "     (Account & Settings -> API Keys), put BOTH halves in .env, re-run."
  exit 1
fi
green "     HTTP 200 — valid. Nothing was created."

bold "3/4  Setting both variables on Railway service '$SERVICE'"

printf '%s' "$KEY_ID"     | railway variable set RAZORPAY_KEY_ID     --stdin --service "$SERVICE" || exit 1
printf '%s' "$KEY_SECRET" | railway variable set RAZORPAY_KEY_SECRET --stdin --service "$SERVICE" || exit 1
green "     Both set. Railway is redeploying."

bold "4/4  Waiting for the redeploy, then verifying production"

for i in $(seq 1 15); do
  sleep 12
  RESULT="$(railway run --service "$SERVICE" "$PY" scripts/diagnose_payments.py 2>/dev/null | grep -E '^RESULT' || true)"
  case "$RESULT" in
    *working*)
      green ""
      green "     $RESULT"
      green "     Payments are live. Try the Upgrade button."
      exit 0 ;;
    *BROKEN*|*DISABLED*)
      echo "     attempt $i: not live yet ($RESULT)" ;;
    *)
      echo "     attempt $i: deploy still starting…" ;;
  esac
done

red ""
red "     Variables are set, but production has not reported healthy yet."
red "     Give the deploy another minute, then run:"
red "       railway run python scripts/diagnose_payments.py"
exit 1
