import uuid
from datetime import date
from pathlib import Path
from typing import List

import aiofiles
from fastapi import APIRouter, Request, Depends, Form, Query, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func, or_

from database.connection import get_db
from database.models import Doctor, Patient, Appointment, AppointmentStatus, PatientNote, NoteFile
from services.auth_service import get_paying_doctor, require_pin

router = APIRouter(prefix="/patients", tags=["patients"])
templates = Jinja2Templates(directory="templates")

MAX_FILE_BYTES = 10 * 1024 * 1024   # 10 MB


# --------------------------------------------------------------------------- #
#  Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _ordinal(n: int) -> str:
    """Return ordinal string: 1 → '1st', 23 → '23rd', etc."""
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    elif n % 10 == 1:
        suffix = "st"
    elif n % 10 == 2:
        suffix = "nd"
    elif n % 10 == 3:
        suffix = "rd"
    else:
        suffix = "th"
    return f"{n}{suffix}"


def _date_label(dt) -> str:
    """Format a datetime as '23rd April, 2026'."""
    from datetime import datetime as _dt
    if not dt:
        return ""
    if isinstance(dt, str):
        dt = _dt.fromisoformat(dt)
    return f"{_ordinal(dt.day)} {dt.strftime('%B, %Y')}"


def _fmt_size(b: int | None) -> str:
    if not b:
        return ""
    if b < 1024:
        return f"{b} B"
    if b < 1024 * 1024:
        return f"{b / 1024:.1f} KB"
    return f"{b / (1024*1024):.1f} MB"


def _notes_data(patient_notes) -> list:
    """Convert PatientNote ORM objects → plain dicts for templates / JSON."""
    out = []
    for n in patient_notes:
        out.append({
            "id":         n.id,
            "text":       n.note_text,
            "date_label": _date_label(n.created_at),
            "files": [
                {
                    "id":   f.id,
                    "name": f.original_name,
                    "size": _fmt_size(f.file_size),
                }
                for f in (n.files or [])
            ],
        })
    return out


def _upload_dir(doctor_id: int, patient_id: int) -> Path:
    p = Path(f"uploads/patients/{doctor_id}/{patient_id}")
    p.mkdir(parents=True, exist_ok=True)
    return p


# --------------------------------------------------------------------------- #
#  List                                                                         #
# --------------------------------------------------------------------------- #

@router.get("", response_class=HTMLResponse)
def patients_list(
    request: Request,
    q: str = Query(default=""),
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    query = db.query(Patient).filter(Patient.doctor_id == doctor.id)

    if q.strip():
        term = f"%{q.strip()}%"
        query = query.filter(
            or_(Patient.name.ilike(term), Patient.phone.ilike(term))
        )

    patients = query.order_by(
        Patient.last_visit.desc(), Patient.created_at.desc()
    ).all()

    total = db.query(func.count(Patient.id)).filter(
        Patient.doctor_id == doctor.id
    ).scalar()

    return templates.TemplateResponse(request, "patients.html", {
        "doctor":   doctor,
        "patients": patients,
        "total":    total,
        "q":        q,
        "active":   "patients",
    })


# --------------------------------------------------------------------------- #
#  Detail                                                                       #
# --------------------------------------------------------------------------- #

@router.get("/{patient_id}", response_class=HTMLResponse)
def patient_detail(
    patient_id: int,
    request: Request,
    doctor: Doctor = Depends(require_pin),
    db: Session = Depends(get_db),
):
    patient = db.query(Patient).filter(
        Patient.id == patient_id,
        Patient.doctor_id == doctor.id,
    ).first()
    if not patient:
        return RedirectResponse(url="/patients", status_code=303)

    # ── One-time migration: move legacy patient.notes → PatientNote row ──
    if patient.notes:
        existing_count = db.query(func.count(PatientNote.id)).filter(
            PatientNote.patient_id == patient.id
        ).scalar()
        if existing_count == 0:
            legacy = PatientNote(
                patient_id=patient.id,
                doctor_id=doctor.id,
                note_text=patient.notes,
                created_at=patient.created_at,   # approximate original date
            )
            db.add(legacy)
            patient.notes = None
            db.commit()

    appointments = (
        db.query(Appointment)
        .filter(
            Appointment.patient_id == patient.id,
            Appointment.doctor_id  == doctor.id,
        )
        .order_by(
            Appointment.appointment_date.desc(),
            Appointment.appointment_time.desc(),
        )
        .all()
    )

    raw_notes = (
        db.query(PatientNote)
        .filter(PatientNote.patient_id == patient.id, PatientNote.doctor_id == doctor.id)
        .order_by(PatientNote.created_at.desc())
        .all()
    )

    completed = sum(1 for a in appointments if a.status == AppointmentStatus.completed)
    upcoming  = sum(1 for a in appointments if a.status == AppointmentStatus.scheduled
                    and a.appointment_date >= date.today())

    return templates.TemplateResponse(request, "patient_detail.html", {
        "doctor":       doctor,
        "patient":      patient,
        "appointments": appointments,
        "notes_data":   _notes_data(raw_notes),
        "completed":    completed,
        "upcoming":     upcoming,
        "active":       "patients",
        "pin_required": getattr(request.state, "pin_required", False),
    })


# --------------------------------------------------------------------------- #
#  Add Note (AJAX — multipart: text + optional files)                          #
# --------------------------------------------------------------------------- #

@router.post("/{patient_id}/notes/add")
async def add_note(
    patient_id: int,
    note_text: str = Form(""),
    files: List[UploadFile] = File(default=[]),
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    text = note_text.strip()
    real_files = [f for f in files if f.filename]

    if not text and not real_files:
        return JSONResponse({"error": "Note cannot be empty."}, status_code=400)

    # Create the note row
    note = PatientNote(
        patient_id=patient_id,
        doctor_id=doctor.id,
        note_text=text or "(files attached)",
    )
    db.add(note)
    db.flush()   # populate note.id before inserting files

    saved_files = []
    udir = _upload_dir(doctor.id, patient_id)

    for f in real_files:
        content = await f.read()
        if len(content) > MAX_FILE_BYTES:
            continue   # silently skip oversized files

        stored_name = f"{uuid.uuid4().hex}_{f.filename}"
        dest = udir / stored_name
        async with aiofiles.open(dest, "wb") as fh:
            await fh.write(content)

        nf = NoteFile(
            note_id=note.id,
            original_name=f.filename,
            stored_name=stored_name,
            file_size=len(content),
        )
        db.add(nf)
        db.flush()
        saved_files.append({
            "id":   nf.id,
            "name": f.filename,
            "size": _fmt_size(len(content)),
        })

    db.commit()

    return JSONResponse({
        "note_id":    note.id,
        "date_label": _date_label(note.created_at),
        "text":       note.note_text,
        "files":      saved_files,
    })


# --------------------------------------------------------------------------- #
#  Download File                                                                #
# --------------------------------------------------------------------------- #

@router.get("/{patient_id}/files/{file_id}")
def view_file(
    patient_id: int,
    file_id: int,
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    import mimetypes
    nf = db.query(NoteFile).join(PatientNote).filter(
        NoteFile.id == file_id,
        PatientNote.doctor_id == doctor.id,
        PatientNote.patient_id == patient_id,
    ).first()
    if not nf:
        return JSONResponse({"error": "File not found."}, status_code=404)

    path = Path(f"uploads/patients/{doctor.id}/{patient_id}/{nf.stored_name}")
    if not path.exists():
        return JSONResponse({"error": "File missing on disk."}, status_code=404)

    mime_type, _ = mimetypes.guess_type(nf.original_name)
    if not mime_type:
        mime_type = "application/octet-stream"

    # Browser-previewable types get inline; everything else forces download
    previewable = (
        mime_type.startswith("image/") or
        mime_type.startswith("text/")  or
        mime_type == "application/pdf"
    )
    disposition = "inline" if previewable else "attachment"

    safe_name = nf.original_name.replace('"', '')
    return FileResponse(
        path=str(path),
        media_type=mime_type,
        headers={"Content-Disposition": f'{disposition}; filename="{safe_name}"'},
    )


# --------------------------------------------------------------------------- #
#  Delete Note                                                                  #
# --------------------------------------------------------------------------- #

@router.post("/{patient_id}/notes/{note_id}/delete")
def delete_note(
    patient_id: int,
    note_id: int,
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    note = db.query(PatientNote).filter(
        PatientNote.id == note_id,
        PatientNote.patient_id == patient_id,
        PatientNote.doctor_id == doctor.id,
    ).first()
    if note:
        # Delete physical files from disk
        udir = Path(f"uploads/patients/{doctor.id}/{patient_id}")
        for nf in note.files:
            p = udir / nf.stored_name
            if p.exists():
                p.unlink()
        db.delete(note)
        db.commit()
    return JSONResponse({"ok": True})


# --------------------------------------------------------------------------- #
#  Delete Patient                                                               #
# --------------------------------------------------------------------------- #

@router.post("/{patient_id}/delete")
def delete_patient(
    patient_id: int,
    request: Request,
    doctor: Doctor = Depends(require_pin),
    db: Session = Depends(get_db),
):
    patient = db.query(Patient).filter(
        Patient.id == patient_id,
        Patient.doctor_id == doctor.id,
    ).first()
    if patient:
        # Remove upload directory for this patient
        import shutil
        udir = Path(f"uploads/patients/{doctor.id}/{patient_id}")
        if udir.exists():
            shutil.rmtree(udir, ignore_errors=True)
        db.query(Appointment).filter(Appointment.patient_id == patient.id).delete()
        db.delete(patient)
        db.commit()
    return RedirectResponse(url="/patients", status_code=303)


# --------------------------------------------------------------------------- #
#  Edit Patient                                                                 #
# --------------------------------------------------------------------------- #

@router.post("/{patient_id}/edit")
def edit_patient(
    patient_id: int,
    name: str = Form(...),
    phone: str = Form(...),
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    patient = db.query(Patient).filter(
        Patient.id == patient_id,
        Patient.doctor_id == doctor.id,
    ).first()
    if patient:
        patient.name  = name.strip()
        patient.phone = phone.strip()
        db.commit()
    return RedirectResponse(url=f"/patients/{patient_id}", status_code=303)


# --------------------------------------------------------------------------- #
#  Legacy single-note update (kept for backwards compat, now unused by UI)     #
# --------------------------------------------------------------------------- #

@router.post("/{patient_id}/notes", response_class=HTMLResponse)
def update_notes(
    patient_id: int,
    notes: str = Form(""),
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    patient = db.query(Patient).filter(
        Patient.id == patient_id,
        Patient.doctor_id == doctor.id,
    ).first()
    if patient:
        patient.notes = notes.strip() or None
        db.commit()
    return RedirectResponse(url=f"/patients/{patient_id}", status_code=303)
