"""
diagnose_email.py — why didn't that email arrive?

Email failures in this app are silent by design: send_email never raises, so a
misconfigured sender looks exactly like a working one from the outside. This
prints every input that decides whether a message gets delivered, and can send
exactly one real message when you explicitly ask it to.

    python scripts/diagnose_email.py                  # config + DNS only, sends nothing
    python scripts/diagnose_email.py --send you@x.com # sends ONE email, prints Resend's raw reply

On Railway, run it against production's own environment variables:

    railway run python scripts/diagnose_email.py

Nothing here sends mail unless --send is passed, so it costs no quota.
"""
import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings                      # noqa: E402
from services.url_service import public_base_url  # noqa: E402


def _dig(record_type: str, name: str) -> list[str]:
    try:
        out = subprocess.run(["dig", "+short", record_type, name],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception as exc:
        return [f"<lookup failed: {exc}>"]
    return [line for line in out.strip().splitlines() if line]


def _sender_domain() -> str:
    """Domain out of `Name <addr@domain>` or a bare address."""
    raw = (settings.EMAIL_FROM or "").strip()
    if "<" in raw and ">" in raw:
        raw = raw[raw.index("<") + 1:raw.index(">")]
    return raw.split("@")[-1].strip().lower() if "@" in raw else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", metavar="EMAIL",
                    help="send one real test email to this address (uses 1 of the daily quota)")
    args = ap.parse_args()

    key = settings.RESEND_API_KEY or ""
    print("\n=== Configuration ===")
    # Only the public prefix — never print a full credential.
    print(f"  RESEND_API_KEY      : {'set (' + key[:8] + '…, ' + str(len(key)) + ' chars)' if key else 'NOT SET  <-- no email can ever be sent'}")
    print(f"  EMAIL_FROM          : {settings.EMAIL_FROM or 'NOT SET'}")
    print(f"  EMAIL_REPLY_TO      : {settings.EMAIL_REPLY_TO or '(none)'}")
    print(f"  ENVIRONMENT         : {settings.ENVIRONMENT}")
    print(f"  PUBLIC_BASE_URL     : {settings.PUBLIC_BASE_URL}")
    print(f"  RAILWAY_PUBLIC_DOMAIN: {os.environ.get('RAILWAY_PUBLIC_DOMAIN') or '(not on Railway)'}")
    print(f"  -> links will use   : {public_base_url()}")

    try:
        import resend  # noqa: F401
        print("  resend package      : installed")
    except ImportError:
        print("  resend package      : NOT INSTALLED  <-- pip install resend")

    domain = _sender_domain()
    print(f"\n=== Sending domain: {domain or '(could not parse EMAIL_FROM)'} ===")
    if domain:
        # Resend's required DNS set. Missing any of these means Resend rejects
        # every send from this domain with a 403, which the app then swallows.
        checks = [
            ("DKIM  (resend._domainkey TXT)", _dig("TXT", f"resend._domainkey.{domain}")),
            (f"SPF   (send.{domain} TXT)",    _dig("TXT", f"send.{domain}")),
            (f"MX    (send.{domain})",        _dig("MX", f"send.{domain}")),
            ("DMARC (_dmarc TXT)",            _dig("TXT", f"_dmarc.{domain}")),
        ]
        for label, values in checks:
            if values:
                print(f"  [ok]      {label}: {values[0][:80]}")
            else:
                print(f"  [MISSING] {label}  <-- Resend will reject sends from this domain")

    base_host = public_base_url().replace("https://", "").replace("http://", "").split("/")[0]
    print(f"\n=== Link host: {base_host} ===")
    if base_host and not (_dig("A", base_host) or _dig("CNAME", base_host)):
        print(f"  [BROKEN] {base_host} has no A or CNAME record — every emailed link 404s.")
    elif base_host:
        print("  [ok]      resolves")

    if not args.send:
        print("\nNo email sent. Re-run with --send <address> to send exactly one test.\n")
        return 0

    print(f"\n=== Sending one test email to {args.send} ===")
    from services.email_service import send_email, render_email
    ok, detail = send_email(
        to=args.send,
        subject="Med Track email delivery test",
        html=render_email("<p>If you are reading this, Med Track email delivery works.</p>",
                          footer_html="Sent by scripts/diagnose_email.py."),
    )
    print(f"  ok     : {ok}")
    print(f"  detail : {detail}")
    if not ok:
        print("\n  Common causes:")
        print("   'not configured'          -> RESEND_API_KEY is missing in this environment")
        print("   'domain is not verified'  -> verify the sending domain at resend.com/domains")
        print("   'You can only send to...' -> unverified domain; Resend restricts you to your own address")
        print("   'Too many requests'       -> daily quota exhausted")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
