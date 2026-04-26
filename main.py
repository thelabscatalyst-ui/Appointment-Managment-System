from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from contextlib import asynccontextmanager
from pathlib import Path

from database.connection import create_tables
from routers import auth, appointments, doctors, patients, public, admin, clinic
from services.scheduler_service import start_scheduler, stop_scheduler
from services.auth_service import PlanExpired, PinRequired


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

templates = Jinja2Templates(directory="templates")

app.include_router(auth.router)
app.include_router(appointments.router)
app.include_router(doctors.router)
app.include_router(patients.router)
app.include_router(public.router)
app.include_router(admin.router)
app.include_router(clinic.router)


@app.exception_handler(401)
async def unauthorized_handler(request: Request, exc: HTTPException):
    return RedirectResponse(url="/login", status_code=303)


@app.exception_handler(403)
async def forbidden_handler(request: Request, exc: HTTPException):
    return RedirectResponse(url="/dashboard", status_code=303)


@app.exception_handler(PlanExpired)
async def plan_expired_handler(request: Request, exc: PlanExpired):
    return RedirectResponse(url="/billing", status_code=303)


@app.exception_handler(PinRequired)
async def pin_required_handler(request: Request, exc: PinRequired):
    # Redirect non-GET (form POSTs) directly to the parent GET page.
    # That page will render with pin_required=True and show the blur overlay.
    return RedirectResponse(url=exc.return_url, status_code=303)


@app.get("/")
def root():
    return RedirectResponse(url="/login")
