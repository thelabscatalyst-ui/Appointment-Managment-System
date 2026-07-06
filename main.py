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
from services.auth_service import PlanExpired, PinRequired, decode_token
from config import settings

# ── Login rate limiter — max 10 attempts per IP per 15 minutes ──────────────
_LOGIN_WINDOW  = 15 * 60   # 15 minutes in seconds
_LOGIN_MAX     = 10        # max attempts per window
_login_attempts: dict[str, list[float]] = collections.defaultdict(list)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure file-upload directory exists
    Path("uploads/patients").mkdir(parents=True, exist_ok=True)
    create_tables()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="ClinicOS", version="1.0.0", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")


_PUBLIC_PREFIXES = (
    "/login", "/register", "/pricing", "/book/", "/queue/",
    "/static/", "/doctor-invite/", "/plan-lapsed", "/auth/", "/feedback/",
)

@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Add security headers to every response."""
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
        "frame-src https://api.razorpay.com https://checkout.razorpay.com; "
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
    if request.method == "POST" and request.url.path in ("/login", "/pin-prompt"):
        ip  = request.client.host if request.client else "unknown"
        now = time.time()
        # Purge old timestamps outside the window
        _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < _LOGIN_WINDOW]
        if len(_login_attempts[ip]) >= _LOGIN_MAX:
            from fastapi.responses import HTMLResponse as _HTML
            from fastapi.templating import Jinja2Templates as _Tmpl
            _t = _Tmpl(directory="templates")
            retry_secs = int(_LOGIN_WINDOW - (now - _login_attempts[ip][0]))
            retry_mins = max(1, retry_secs // 60)
            return _HTML(
                f'<meta http-equiv="refresh" content="5;url=/login">'
                f'<p style="font-family:sans-serif;padding:40px;color:#9a8f85;">'
                f'Too many login attempts. Try again in {retry_mins} minute(s).</p>',
                status_code=429,
            )
        _login_attempts[ip].append(now)
    return await call_next(request)


@app.middleware("http")
async def inject_clinic_owner_state(request: Request, call_next):
    """Sets request.state.is_clinic_owner so base.html navbar can show Clinic Admin link."""
    request.state.is_clinic_owner = False
    token = request.cookies.get("access_token")
    if token:
        payload = decode_token(token)
        if payload and payload.get("doctor_id"):
            try:
                from database.connection import SessionLocal
                from database.models import ClinicDoctor, Clinic
                db = SessionLocal()
                try:
                    owns = (
                        db.query(ClinicDoctor)
                        .join(Clinic, Clinic.id == ClinicDoctor.clinic_id)
                        .filter(
                            ClinicDoctor.doctor_id == payload["doctor_id"],
                            ClinicDoctor.role == "owner",
                            ClinicDoctor.is_active == True,
                            Clinic.plan_type == "clinic",
                        )
                        .first()
                    )
                    request.state.is_clinic_owner = owns is not None
                finally:
                    db.close()
            except Exception:
                pass
    response = await call_next(request)
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
    content = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://www.clinicos.store/</loc>
    <priority>1.0</priority>
    <changefreq>weekly</changefreq>
  </url>
  <url>
    <loc>https://www.clinicos.store/register</loc>
    <priority>0.9</priority>
    <changefreq>monthly</changefreq>
  </url>
  <url>
    <loc>https://www.clinicos.store/login</loc>
    <priority>0.7</priority>
    <changefreq>monthly</changefreq>
  </url>
  <url>
    <loc>https://www.clinicos.store/pricing</loc>
    <priority>0.8</priority>
    <changefreq>monthly</changefreq>
  </url>
</urlset>"""
    return Response(content=content, media_type="application/xml")


@app.get("/robots.txt", include_in_schema=False)
def robots():
    content = """User-agent: *
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

Sitemap: https://www.clinicos.store/sitemap.xml"""
    return PlainTextResponse(content=content)


@app.exception_handler(401)
async def unauthorized_handler(request: Request, exc: HTTPException):
    path = request.url.path
    # JSON consumers and /auth/* endpoints get a plain 401, not a redirect
    accept = request.headers.get("accept", "")
    if path.startswith("/auth/") or "application/json" in accept:
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    next_url = quote(path, safe="/")
    return RedirectResponse(url=f"/login?next={next_url}", status_code=303)


@app.get("/auth/check")
async def auth_check(request: Request):
    """Lightweight session validity check — JWT decode only, no DB lookup."""
    token = request.cookies.get("access_token")
    if not token or not decode_token(token):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return JSONResponse({"ok": True})


@app.exception_handler(403)
async def forbidden_handler(request: Request, exc: HTTPException):
    return RedirectResponse(url="/dashboard", status_code=303)


@app.exception_handler(PlanExpired)
async def plan_expired_handler(request: Request, exc: PlanExpired):
    # Associates and clinic-plan doctors can't renew themselves — show lapsed page
    if getattr(exc, "reason", "personal") == "clinic":
        return RedirectResponse(url="/plan-lapsed", status_code=303)
    return RedirectResponse(url="/billing", status_code=303)


@app.get("/plan-lapsed")
async def plan_lapsed_page(request: Request):
    return templates.TemplateResponse(request, "plan_lapsed.html", {})


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
