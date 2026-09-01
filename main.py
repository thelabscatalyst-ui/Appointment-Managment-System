# ── Force the whole process to India Standard Time ─────────────────────────
# Railway servers run in UTC; without this every date.today()/datetime.now()
# is 5.5 hours behind IST, which shows the wrong day's queue and visit times.
import os
import time
os.environ["TZ"] = "Asia/Kolkata"
try:
    time.tzset()  # applies TZ to the running process (Unix/Linux — Railway & macOS)
except AttributeError:
    pass  # Windows has no tzset()

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse, Response, PlainTextResponse
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote
import collections

from database.connection import create_tables
from routers import auth, appointments, doctors, patients, public, admin, clinic, visits, billing_ops, income, prescriptions, feedback
from services.scheduler_service import start_scheduler, stop_scheduler
from services.auth_service import (
    PlanExpired, PinRequired, OwnerOnly, EmailNotVerified, decode_token, create_access_token, should_renew,
)
from routers.clinic import ClinicAdminAuthRequired
from config import settings

# ── Auth rate limiter — max 10 attempts per client IP per 15 minutes ────────
_LOGIN_WINDOW  = 15 * 60   # 15 minutes in seconds
_LOGIN_MAX     = 10        # max attempts per window
_login_attempts: dict[str, list[float]] = collections.defaultdict(list)

# Sensitive auth endpoints. Prefix-matched, so future routes (/verify-email,
# /reset-password/<token>) are covered without touching this list again.
_RATE_LIMITED_PREFIXES = (
    "/login",
    "/register",
    "/pin-prompt",
    "/forgot-password",
    "/reset-password",
    "/verify-email",
)


def _client_ip(request: Request) -> str:
    """Best-effort real client IP, correct behind Railway's proxy.

    `request.client.host` is the *proxy's* address on Railway — production
    logs show 100.64.0.0/10 (CGNAT), which is their internal network. Keying
    the rate limiter on that pools unrelated doctors into shared buckets:
    ten failed logins by anyone could lock out everyone sharing that proxy IP,
    while an attacker rotating across the pool gets 10x the attempts.

    X-Forwarded-For reads left-to-right as client -> ...-> last proxy. A client
    can forge the left-hand entries, but not the ones our edge appends, so we
    walk from the RIGHT and take the first genuinely public address. Internal
    hops (private/loopback/CGNAT) are skipped.
    """
    import ipaddress

    candidates: list[str] = []
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        candidates.extend(part.strip() for part in xff.split(","))
    real_ip = request.headers.get("x-real-ip", "")
    if real_ip:
        candidates.append(real_ip.strip())

    for raw in reversed(candidates):
        if not raw:
            continue
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        # Skip infra hops; 100.64.0.0/10 is CGNAT and is_private covers it
        # on modern Python, but check explicitly for older behaviour.
        if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local:
            continue
        if ip.version == 4 and ipaddress.ip_address("100.64.0.0") <= ip <= ipaddress.ip_address("100.127.255.255"):
            continue
        return str(ip)

    # Nothing public found — fall back to the socket peer. Better than
    # "unknown", which would collapse every caller into one bucket.
    return request.client.host if request.client else "unknown"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure file-upload directory exists
    Path("uploads/patients").mkdir(parents=True, exist_ok=True)
    create_tables()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="Med Track", version="1.0.0", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")


_PUBLIC_PREFIXES = (
    "/login", "/register", "/pricing", "/book/", "/queue/",
    "/static/", "/doctor-invite/", "/plan-lapsed", "/auth/", "/feedback/",
)

@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Add security headers to every response."""
    # Every router builds its own Jinja2Templates, so a template global would
    # reach only one of them. request.state reaches all of them.
    #
    # Skipped for static assets: they render no template, and a logged-in
    # browser fetches a lot of them. The other middleware already skips the
    # same paths for the same reason.
    _p = request.url.path
    if not (_p.startswith("/static/") or _p.startswith("/uploads/")):
        request.state.asset_v = settings.ASSET_VERSION
        from services.payment_service import price_display
        request.state.price = price_display()
    response = await call_next(request)
    response.headers["X-Content-Type-Options"]  = "nosniff"
    response.headers["X-Frame-Options"]          = "DENY"
    response.headers["X-XSS-Protection"]         = "1; mode=block"
    response.headers["Referrer-Policy"]           = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"]        = "geolocation=(), microphone=(), camera=()"
    if settings.ENVIRONMENT.lower() == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Content-Security-Policy"]   = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://www.googletagmanager.com https://www.google-analytics.com "
        "https://checkout.razorpay.com https://cdn.razorpay.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: https:; "
        "connect-src 'self' https://www.google-analytics.com https://region1.google-analytics.com "
        "https://api.razorpay.com https://lumberjack.razorpay.com; "
        "frame-src 'self' https://api.razorpay.com https://checkout.razorpay.com https://www.youtube.com https://youtube.com; "
        "worker-src 'self'; "
        "frame-ancestors 'none';"
    )

    path = request.url.path
    is_public = path == "/" or any(path.startswith(p) for p in _PUBLIC_PREFIXES)
    if not is_public:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        response.headers["Pragma"]        = "no-cache"
        response.headers["Expires"]       = "0"
    return response


@app.middleware("http")
async def login_rate_limit(request: Request, call_next):
    """Block brute-force login/PIN attempts — max 10 per IP per 15 minutes."""
    import sys
    if "pytest" in sys.modules:
        return await call_next(request)

    path = request.url.path
    if request.method == "POST" and any(path.startswith(p) for p in _RATE_LIMITED_PREFIXES):
        ip  = _client_ip(request)
        now = time.time()
        # Purge old timestamps outside the window
        _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < _LOGIN_WINDOW]
        if len(_login_attempts[ip]) >= _LOGIN_MAX:
            from fastapi.responses import HTMLResponse as _HTML
            from fastapi.templating import Jinja2Templates as _Tmpl
            _t = _Tmpl(directory="templates")
            retry_secs = int(_LOGIN_WINDOW - (now - _login_attempts[ip][0]))
            retry_mins = max(1, retry_secs // 60)
            # Bounce back to the page they came from, not always /login.
            back = path if path.startswith("/register") else "/login"
            return _HTML(
                f'<meta http-equiv="refresh" content="5;url={back}">'
                f'<p style="font-family:sans-serif;padding:40px;color:#9a8f85;">'
                f'Too many attempts. Try again in {retry_mins} minute(s).</p>',
                status_code=429,
                headers={"Retry-After": str(max(1, retry_secs))},
            )
        _login_attempts[ip].append(now)
    return await call_next(request)


@app.middleware("http")
async def inject_clinic_owner_state(request: Request, call_next):
    """Sets request.state.is_clinic_owner so base.html navbar can show Clinic Admin link."""
    request.state.is_clinic_owner = False
    request.state.membership_count = 0
    request.state.clinic_memberships = []
    request.state.active_clinic = None
    request.state.active_clinic_id = None
    # Defaulted here so templates on routes that never resolve a clinic (those
    # using get_current_doctor alone) read None instead of raising. None is not
    # 'owner', so the owner-only nav stays hidden — fail closed.
    request.state.active_role = None
    # Support contact, read by templates that offer a human fallback
    # (verify_email.html, plan_lapsed.html). Set here rather than threaded
    # through every route context — same pattern as is_clinic_owner.
    request.state.support_whatsapp = settings.SUPPORT_WHATSAPP

    # Static assets carry the session cookie too, and none of them render a
    # template, so the work below is pure waste on those requests.
    _path = request.url.path
    if _path.startswith("/static/") or _path.startswith("/uploads/") or _path == "/favicon.ico":
        return await call_next(request)

    token = request.cookies.get("access_token")
    payload = None      # must exist for the renewal check below, even if logged out
    if token:
        payload = decode_token(token)
        if payload and payload.get("doctor_id"):
            try:
                from database.connection import SessionLocal
                from database.models import ClinicDoctor, Clinic
                db = SessionLocal()
                try:
                    from services.clinic_context import (
                        is_clinic_owner as _is_clinic_owner,
                        active_memberships as _active_memberships,
                    )
                    request.state.is_clinic_owner = _is_clinic_owner(
                        db, payload["doctor_id"])
                    # Membership count drives whether the clinic switcher is
                    # rendered at all — single-clinic doctors must see no
                    # change anywhere in the UI.
                    _ms = _active_memberships(db, payload["doctor_id"])
                    request.state.membership_count = len(_ms)
                    # Detached plain dicts: the Session closes in the finally
                    # below, so ORM objects would raise DetachedInstanceError
                    # when the template touched a lazy attribute.
                    _clinic_by_id = {}
                    if _ms:
                        from database.models import Clinic as _C
                        # One query for every clinic, not one per membership.
                        _clinic_by_id = {
                            c.id: c for c in db.query(_C).filter(
                                _C.id.in_([m.clinic_id for m in _ms])).all()
                        }
                    if len(_ms) > 1:
                        request.state.clinic_memberships = [
                            {
                                "clinic_id": _m.clinic_id,
                                "role": _m.role,
                                "clinic_name": (
                                    _clinic_by_id[_m.clinic_id].name
                                    if _m.clinic_id in _clinic_by_id
                                    else f"Clinic {_m.clinic_id}"),
                            }
                            for _m in _ms
                        ]

                    # Resolve the active clinic HERE, not only in
                    # get_paying_doctor. Pages that never reach that dependency
                    # — /billing, /plan-lapsed, the paywall a lapsed doctor is
                    # sent to — had no active clinic, so the switcher was hidden
                    # on exactly the pages where a doctor needs it most: an
                    # associate whose own trial expired could not get back to
                    # the clinic that funds them.
                    #
                    # This grants nothing. resolve_active_clinic re-checks live
                    # membership on every call, and the plan gate still runs in
                    # get_paying_doctor; this only decides which clinic the gate
                    # is applied to, and populates the navbar.
                    from services.clinic_context import resolve_active_clinic
                    from database.models import Doctor as _D
                    _doc = db.query(_D).filter(_D.id == payload["doctor_id"]).first()
                    if _doc is not None:
                        _clinic, _mem = resolve_active_clinic(request, _doc, db, _ms)
                        if _clinic is not None:
                            # Detached copy: the Session closes below, so the
                            # template would hit DetachedInstanceError on a
                            # lazy attribute of the live ORM object.
                            db.expunge(_clinic)
                        request.state.active_clinic = _clinic
                        request.state.active_clinic_id = _clinic.id if _clinic else None
                        request.state.active_role = _mem.role if _mem else None
                finally:
                    db.close()
            except Exception:
                pass
    response = await call_next(request)

    # ── Sliding session renewal ──────────────────────────────────────────
    # Sessions are short (ACCESS_TOKEN_EXPIRE_MINUTES), but a doctor runs a
    # 3-4 hour clinic — a hard logout mid-consultation is unacceptable. So an
    # active session gets a fresh expiry once it's over halfway through, while
    # an abandoned one still dies on schedule. should_renew() enforces a
    # 12-hour absolute cap so this can't extend a session forever.
    if token and payload and payload.get("doctor_id"):
        # Only on GET. Auth routes mint their own cookies, and renewing on
        # /logout would re-set the cookie the route just deleted — silently
        # breaking logout.
        already_set = any(
            "access_token=" in h
            for h in response.headers.getlist("set-cookie")
        )
        if request.method == "GET" and not already_set and should_renew(payload):
            renewed = create_access_token({
                k: payload[k]
                for k in ("doctor_id", "tv", "iat", "jti")
                if k in payload
            })
            response.set_cookie(
                key="access_token", value=renewed,
                httponly=True,
                secure=settings.ENVIRONMENT.lower() == "production",
                max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
                samesite="lax",
            )
    return response

templates = Jinja2Templates(directory="templates")

app.include_router(auth.router)
app.include_router(appointments.router)
app.include_router(doctors.router)
app.include_router(patients.router)
app.include_router(public.router)
app.include_router(admin.router)
app.include_router(clinic.router)
app.include_router(visits.router)
app.include_router(billing_ops.router)
app.include_router(income.router)
app.include_router(prescriptions.router)
app.include_router(feedback.router)


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap():
    from services.url_service import public_base_url
    base = public_base_url()
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>{base}/</loc>
    <priority>1.0</priority>
    <changefreq>weekly</changefreq>
  </url>
  <url>
    <loc>{base}/register</loc>
    <priority>0.9</priority>
    <changefreq>monthly</changefreq>
  </url>
  <url>
    <loc>{base}/login</loc>
    <priority>0.7</priority>
    <changefreq>monthly</changefreq>
  </url>
  <url>
    <loc>{base}/pricing</loc>
    <priority>0.8</priority>
    <changefreq>monthly</changefreq>
  </url>
</urlset>"""
    return Response(content=content, media_type="application/xml")


@app.get("/robots.txt", include_in_schema=False)
def robots():
    from services.url_service import public_base_url
    base = public_base_url()
    content = f"""User-agent: *
Allow: /
Allow: /register
Allow: /login
Allow: /pricing
Disallow: /dashboard
Disallow: /patients
Disallow: /appointments
Disallow: /reports
Disallow: /settings
Disallow: /expenses
Disallow: /income
Disallow: /billing
Disallow: /queue
Disallow: /admin

Sitemap: {base}/sitemap.xml"""
    return PlainTextResponse(content=content)


@app.exception_handler(401)
async def unauthorized_handler(request: Request, exc: HTTPException):
    path = request.url.path
    # JSON consumers and /auth/* endpoints get a plain 401, not a redirect
    accept = request.headers.get("accept", "")
    if path.startswith("/auth/") or "application/json" in accept:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    # Only a GET can be resumed after login. Carrying the path of a POST sends
    # the browser to GET it once the session is restored, which 405s on any
    # POST-only route — /clinic/switch being the one users actually hit, via
    # the navbar switcher on a page whose session had expired. The login form
    # keeps `next` in a hidden field, so every retry landed on the same error
    # and login itself looked broken.
    #
    # A POST cannot be meaningfully replayed by a redirect anyway: the body is
    # already gone.
    if request.method != "GET":
        return RedirectResponse(url="/login", status_code=303)

    next_url = quote(path, safe="/")
    return RedirectResponse(url=f"/login?next={next_url}", status_code=303)


@app.get("/auth/check")
async def auth_check(request: Request):
    """Lightweight session validity check — JWT decode only, no DB lookup.

    Explicitly uncacheable. /auth/ is in _PUBLIC_PREFIXES, so the blanket
    no-store the middleware applies to private paths does NOT cover this one —
    and a cached "ok" is worse than useless here: this is what the back/forward
    guard asks after a page is restored from the browser's bfcache, so a stale
    200 would keep showing a logged-out doctor their patients.
    """
    token = request.cookies.get("access_token")
    headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate, private",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    if not token or not decode_token(token):
        return JSONResponse({"ok": False}, status_code=401, headers=headers)
    return JSONResponse({"ok": True}, headers=headers)


@app.exception_handler(403)
async def forbidden_handler(request: Request, exc: HTTPException):
    """403 -> dashboard, but say why.

    This used to discard exc.detail and redirect silently, so an associate
    who clicked or typed /clinic/admin simply landed back on their dashboard
    with no explanation at all.
    """
    reason = getattr(exc, "detail", "") or ""
    if "clinic" in str(reason).lower():
        return RedirectResponse(url="/dashboard?denied=clinic_admin", status_code=303)
    return RedirectResponse(url="/dashboard?denied=1", status_code=303)


@app.exception_handler(ClinicAdminAuthRequired)
async def clinic_admin_auth_required_handler(request: Request, exc):
    """Owner is signed in but hasn't passed the clinic-admin password gate.

    Bounces to the dashboard route, which renders the password prompt in
    place — rather than 403ing, which forbidden_handler would turn into a
    silent redirect with no way to actually authenticate.
    """
    return RedirectResponse(url="/clinic/admin", status_code=303)


@app.exception_handler(EmailNotVerified)
async def email_not_verified_handler(request: Request, exc: EmailNotVerified):
    # Verification is mandatory — no skip option. Every protected route
    # bounces here until the doctor confirms their email.
    return RedirectResponse(url="/verify-email", status_code=303)


@app.exception_handler(PlanExpired)
async def plan_expired_handler(request: Request, exc: PlanExpired):
    # Associates and clinic-plan doctors can't renew themselves — show lapsed page
    if getattr(exc, "reason", "personal") == "clinic":
        return RedirectResponse(url="/plan-lapsed", status_code=303)

    # Deadlock guard: access is now per-clinic, so a doctor whose PERSONAL
    # plan lapsed while their active clinic is their own practice would be
    # bounced to /billing on every request. If they still have live access at
    # some other clinic, say so on a page they can act from, rather than
    # trapping them behind a paywall for a practice they may not want to renew.
    try:
        token = request.cookies.get("access_token")
        payload = decode_token(token) if token else None
        if payload and payload.get("doctor_id"):
            from database.connection import SessionLocal
            from database.models import Doctor as _Doctor
            from services.clinic_context import active_memberships, clinic_plan_active
            from database.models import Clinic as _Clinic
            db = SessionLocal()
            try:
                for m in active_memberships(db, payload["doctor_id"]):
                    c = db.query(_Clinic).filter(_Clinic.id == m.clinic_id).first()
                    if c and clinic_plan_active(c, db):
                        return RedirectResponse(
                            url="/plan-lapsed?other_clinic=1", status_code=303)
            finally:
                db.close()
    except Exception:
        pass

    return RedirectResponse(url="/billing", status_code=303)


@app.get("/plan-lapsed")
async def plan_lapsed_page(request: Request):
    """The paywall for access a doctor cannot renew themselves.

    Lists any OTHER clinic where they still have live, paid access, so a doctor
    whose own plan lapsed is not stranded. Membership is re-read here rather
    than trusted from the ?other_clinic= query flag the handler sets — the flag
    only decides whether we were sent here by the deadlock guard; what a doctor
    may actually reach is always a database question.
    """
    other_clinics = []
    try:
        token = request.cookies.get("access_token")
        payload = decode_token(token) if token else None
        if payload and payload.get("doctor_id"):
            from database.connection import SessionLocal
            from database.models import Clinic as _Clinic
            from services.clinic_context import active_memberships, clinic_plan_active
            db = SessionLocal()
            try:
                active_id = getattr(request.state, "active_clinic_id", None)
                for m in active_memberships(db, payload["doctor_id"]):
                    if m.clinic_id == active_id:
                        continue          # the one they are already stuck on
                    c = db.query(_Clinic).filter(_Clinic.id == m.clinic_id).first()
                    if c and clinic_plan_active(c, db):
                        other_clinics.append({"id": c.id, "name": c.name})
            finally:
                db.close()
    except Exception:
        # Never let this page fail: it is where doctors land when other things
        # have already gone wrong.
        other_clinics = []

    return templates.TemplateResponse(request, "plan_lapsed.html",
                                      {"other_clinics": other_clinics})


@app.exception_handler(OwnerOnly)
async def owner_only_handler(request: Request, exc: OwnerOnly):
    """An associate reached an owner-only route. Send them home with a reason.

    303 for every method: these are whole pages, and a GET that 307'd would
    re-issue the blocked request at the new URL.
    """
    return RedirectResponse(url=f"{exc.return_url}?denied=owner_only", status_code=303)


@app.exception_handler(PinRequired)
async def pin_required_handler(request: Request, exc: PinRequired):
    # Redirect non-GET (form POSTs) directly to the parent GET page.
    # That page will render with pin_required=True and show the blur overlay.
    return RedirectResponse(url=exc.return_url, status_code=303)


@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    token = request.cookies.get("access_token")
    if token and decode_token(token):
        return RedirectResponse(url="/dashboard", status_code=303)
    return templates.TemplateResponse(request, "landing.html", {})
