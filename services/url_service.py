"""
url_service.py — one place that decides what this app's public URL is.

Every emailed link (clinic invites, password resets, feedback links) needs an
absolute URL, and getting it wrong is silent: the mail arrives, the link 404s,
and nothing in the logs says why. That is exactly what happened — the
configured PUBLIC_BASE_URL pointed at a domain with no DNS record at all, so
every reset and feedback link ever sent was dead on arrival.

Two entry points, because the trust model differs:

  * `request_base_url(request)` — derived from the live request. Correct even
    behind Railway's proxy, which terminates TLS and forwards plain HTTP, so
    `request.base_url` alone reports http:// for an https:// site. Only use
    this on routes where the caller is already authenticated: the Host header
    is attacker-controlled, so a link built from it can be pointed anywhere.
    For an invite that is harmless (the clinic owner would only be poisoning
    their own link); for a password reset it would be a live account-takeover
    vector.

  * `public_base_url()` — no request involved. Prefers RAILWAY_PUBLIC_DOMAIN,
    which the platform injects and no HTTP client can influence, and falls
    back to the configured PUBLIC_BASE_URL. This is what unauthenticated and
    background-task link builders must use.
"""
import os

from config import settings


def _railway_origin() -> str | None:
    """Public origin from the platform's own environment, if deployed there.

    Railway injects RAILWAY_PUBLIC_DOMAIN (e.g. "web-production-x.up.railway.app")
    into every deploy. It is always https and always the real external host,
    which makes it a better default than a hand-set value that can rot when a
    domain lapses or was never registered.
    """
    domain = (os.environ.get("RAILWAY_PUBLIC_DOMAIN") or "").strip()
    if not domain:
        return None
    domain = domain.replace("https://", "").replace("http://", "").strip("/")
    return f"https://{domain}" if domain else None


def public_base_url() -> str:
    """Absolute origin for links built without a request in hand."""
    return (_railway_origin() or settings.PUBLIC_BASE_URL or "").rstrip("/")


def request_base_url(request) -> str:
    """Absolute origin for the request being served, proxy-aware.

    Railway (like any TLS-terminating proxy) speaks plain HTTP to the app, so
    Starlette sees scheme "http". Uvicorn only rewrites that from
    X-Forwarded-Proto when the proxy's IP is in --forwarded-allow-ips, which
    it is not here, so the header is read directly. X-Forwarded-Proto may be a
    comma-separated chain; the first entry is the original client's scheme.
    """
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    scheme = forwarded_proto if forwarded_proto in ("http", "https") else request.url.scheme

    # X-Forwarded-Host, then Host. Both are client-controllable — see module
    # docstring for why that is acceptable only on authenticated routes.
    host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    if not host:
        host = (request.headers.get("host") or "").strip()
    if not host:
        return str(request.base_url).rstrip("/")

    return f"{scheme}://{host}"
