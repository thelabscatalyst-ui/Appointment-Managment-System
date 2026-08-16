"""
routers/prescriptions.py — e-Prescription module

Routes:
  GET  /prescriptions/new                      Create form (query: visit_id, patient_id)
  POST /prescriptions/new                      Save new prescription
  GET  /prescriptions/{id}                     View prescription
  GET  /prescriptions/{id}/edit                Edit form
  POST /prescriptions/{id}/edit                Save edits
  GET  /prescriptions/{id}/print               Print-friendly view (no navbar)
  POST /prescriptions/{id}/delete              Delete prescription
  GET  /patients/{patient_id}/prescriptions    All prescriptions for a patient
"""

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Request, Depends, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database.connection import get_db
from database.models import Doctor, Patient, Visit, Prescription, PrescriptionItem
from services.auth_service import get_paying_doctor

router = APIRouter(tags=["prescriptions"])
templates = Jinja2Templates(directory="templates")


# --------------------------------------------------------------------------- #
#  Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _get_prescription_or_404(rx_id: int, doctor_id: int, db: Session) -> Optional[Prescription]:
    """Fetch a prescription belonging to the current doctor, or return None."""
    return db.query(Prescription).filter(
        Prescription.id == rx_id,
        Prescription.doctor_id == doctor_id,
    ).first()


def _parse_rx_items(
    drug_names: List[str],
    dosages: List[str],
    frequencies: List[str],
    durations: List[str],
    instructions_list: List[str],
    notes_list: List[str] = None,
) -> List[dict]:
    """Zip multi-value form fields into a list of item dicts, skipping blank rows."""
    notes_list = notes_list or []
    items = []
    # Pad shorter lists to match the longest
    max_len = max(
        len(drug_names), len(dosages), len(frequencies),
        len(durations), len(instructions_list), len(notes_list),
        1,
    )
    def _pad(lst, length):
        return lst + [""] * (length - len(lst))

    drug_names         = _pad(drug_names, max_len)
    dosages            = _pad(dosages, max_len)
    frequencies        = _pad(frequencies, max_len)
    durations          = _pad(durations, max_len)
    instructions_list  = _pad(instructions_list, max_len)
    notes_list         = _pad(notes_list, max_len)

    for i in range(max_len):
        name = drug_names[i].strip()
        if not name:
            continue
        items.append({
            "drug_name":    name,
            "dosage":       dosages[i].strip() or None,
            "frequency":    frequencies[i].strip() or None,
            "duration":     durations[i].strip() or None,
            "instructions": instructions_list[i].strip() or None,
            "notes":        notes_list[i].strip() or None,
        })
    return items


# --------------------------------------------------------------------------- #
#  Create — GET                                                                #
# --------------------------------------------------------------------------- #

@router.get("/prescriptions/new", response_class=HTMLResponse)
def prescription_new_form(
    request: Request,
    visit_id: Optional[int] = Query(None),
    patient_id: Optional[int] = Query(None),
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    visit   = None
    patient = None

    if visit_id:
        visit = db.query(Visit).filter(
            Visit.id == visit_id,
            Visit.doctor_id == doctor.id,
        ).first()
        if visit:
            patient = db.query(Patient).filter(
                Patient.id == visit.patient_id,
                Patient.doctor_id == doctor.id,
            ).first()

    if not patient and patient_id:
        patient = db.query(Patient).filter(
            Patient.id == patient_id,
            Patient.doctor_id == doctor.id,
        ).first()

    if not patient:
        return RedirectResponse(url="/patients", status_code=303)

    # Autosave model: the instance is created the moment the doctor opens
    # this screen — like a new Google Doc — so nothing is ever lost even if
    # they never click an explicit save button. Editing then continues on
    # the /edit route, which this immediately redirects to.
    rx = Prescription(
        doctor_id  = doctor.id,
        patient_id = patient.id,
        visit_id   = visit.id if visit else None,
    )
    db.add(rx)
    db.commit()
    db.refresh(rx)

    return RedirectResponse(url=f"/prescriptions/{rx.id}/edit", status_code=303)


# --------------------------------------------------------------------------- #
#  Create — POST                                                               #
# --------------------------------------------------------------------------- #

@router.post("/prescriptions/new")
async def prescription_create(
    request: Request,
    visit_id: Optional[int]   = Form(None),
    patient_id: int            = Form(...),
    diagnosis: str             = Form(""),
    advice: str                = Form(""),
    follow_up: str             = Form(""),
    drug_name: List[str]       = Form(default=[]),
    dosage: List[str]          = Form(default=[]),
    frequency: List[str]       = Form(default=[]),
    duration: List[str]        = Form(default=[]),
    instructions: List[str]    = Form(default=[]),
    notes: List[str]           = Form(default=[]),
    doctor: Doctor             = Depends(get_paying_doctor),
    db: Session                = Depends(get_db),
):
    # Verify patient belongs to this doctor
    patient = db.query(Patient).filter(
        Patient.id == patient_id,
        Patient.doctor_id == doctor.id,
    ).first()
    if not patient:
        return RedirectResponse(url="/patients", status_code=303)

    # Verify visit (if provided) belongs to this doctor
    if visit_id:
        visit_row = db.query(Visit).filter(
            Visit.id == visit_id,
            Visit.doctor_id == doctor.id,
        ).first()
        if not visit_row:
            visit_id = None

    rx = Prescription(
        doctor_id  = doctor.id,
        patient_id = patient_id,
        visit_id   = visit_id,
        diagnosis  = diagnosis.strip() or None,
        advice     = advice.strip() or None,
        follow_up  = follow_up.strip() or None,
    )
    db.add(rx)
    db.flush()   # get rx.id before inserting items

    items = _parse_rx_items(drug_name, dosage, frequency, duration, instructions, notes)
    for item in items:
        db.add(PrescriptionItem(
            prescription_id = rx.id,
            drug_name       = item["drug_name"],
            dosage          = item["dosage"],
            frequency       = item["frequency"],
            duration        = item["duration"],
            instructions    = item["instructions"],
            notes           = item["notes"],
        ))

    db.commit()
    return RedirectResponse(url=f"/prescriptions/{rx.id}", status_code=303)


# --------------------------------------------------------------------------- #
#  Detail — GET                                                                #
# --------------------------------------------------------------------------- #

@router.get("/prescriptions/{rx_id}", response_class=HTMLResponse)
def prescription_detail(
    rx_id: int,
    request: Request,
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    rx = _get_prescription_or_404(rx_id, doctor.id, db)
    if not rx:
        return RedirectResponse(url="/patients", status_code=303)

    patient = db.query(Patient).filter(Patient.id == rx.patient_id).first()
    visit   = db.query(Visit).filter(Visit.id == rx.visit_id).first() if rx.visit_id else None

    return templates.TemplateResponse(request, "prescription_detail.html", {
        "doctor":       doctor,
        "prescription": rx,
        "patient":      patient,
        "visit":        visit,
        "active":       "patients",
    })


# --------------------------------------------------------------------------- #
#  Print — GET (no navbar, clean A5 layout)                                   #
# --------------------------------------------------------------------------- #

@router.get("/prescriptions/{rx_id}/print", response_class=HTMLResponse)
def prescription_print(
    rx_id: int,
    request: Request,
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    rx = _get_prescription_or_404(rx_id, doctor.id, db)
    if not rx:
        return RedirectResponse(url="/patients", status_code=303)

    patient = db.query(Patient).filter(Patient.id == rx.patient_id).first()
    visit   = db.query(Visit).filter(Visit.id == rx.visit_id).first() if rx.visit_id else None

    return templates.TemplateResponse(request, "prescription_print.html", {
        "doctor":       doctor,
        "prescription": rx,
        "patient":      patient,
        "visit":        visit,
    })


# --------------------------------------------------------------------------- #
#  Edit — GET                                                                  #
# --------------------------------------------------------------------------- #

@router.get("/prescriptions/{rx_id}/edit", response_class=HTMLResponse)
def prescription_edit_form(
    rx_id: int,
    request: Request,
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    rx = _get_prescription_or_404(rx_id, doctor.id, db)
    if not rx:
        return RedirectResponse(url="/patients", status_code=303)

    patient = db.query(Patient).filter(Patient.id == rx.patient_id).first()
    visit   = db.query(Visit).filter(Visit.id == rx.visit_id).first() if rx.visit_id else None

    return templates.TemplateResponse(request, "prescription_new.html", {
        "doctor":       doctor,
        "patient":      patient,
        "visit":        visit,
        "prescription": rx,   # not None = edit mode
        "active":       "patients",
    })


# --------------------------------------------------------------------------- #
#  Edit — POST                                                                 #
# --------------------------------------------------------------------------- #

@router.post("/prescriptions/{rx_id}/edit")
async def prescription_edit_save(
    rx_id: int,
    request: Request,
    diagnosis: str          = Form(""),
    advice: str             = Form(""),
    follow_up: str          = Form(""),
    drug_name: List[str]    = Form(default=[]),
    dosage: List[str]       = Form(default=[]),
    frequency: List[str]    = Form(default=[]),
    duration: List[str]     = Form(default=[]),
    instructions: List[str] = Form(default=[]),
    notes: List[str]        = Form(default=[]),
    doctor: Doctor          = Depends(get_paying_doctor),
    db: Session             = Depends(get_db),
):
    rx = _get_prescription_or_404(rx_id, doctor.id, db)
    if not rx:
        return RedirectResponse(url="/patients", status_code=303)

    rx.diagnosis  = diagnosis.strip() or None
    rx.advice     = advice.strip() or None
    rx.follow_up  = follow_up.strip() or None
    rx.updated_at = datetime.now()

    # Replace all items
    db.query(PrescriptionItem).filter(
        PrescriptionItem.prescription_id == rx.id
    ).delete(synchronize_session=False)

    items = _parse_rx_items(drug_name, dosage, frequency, duration, instructions, notes)
    for item in items:
        db.add(PrescriptionItem(
            prescription_id = rx.id,
            drug_name       = item["drug_name"],
            dosage          = item["dosage"],
            frequency       = item["frequency"],
            duration        = item["duration"],
            instructions    = item["instructions"],
            notes           = item["notes"],
        ))

    db.commit()
    return RedirectResponse(url=f"/prescriptions/{rx.id}", status_code=303)


# --------------------------------------------------------------------------- #
#  Autosave — POST (JSON, fired continuously while writing)                   #
# --------------------------------------------------------------------------- #

@router.post("/prescriptions/{rx_id}/autosave")
async def prescription_autosave(
    rx_id: int,
    request: Request,
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    rx = _get_prescription_or_404(rx_id, doctor.id, db)
    if not rx:
        return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad_json"}, status_code=400)

    rx.diagnosis  = (payload.get("diagnosis") or "").strip() or None
    rx.advice     = (payload.get("advice") or "").strip() or None
    rx.follow_up  = (payload.get("follow_up") or "").strip() or None
    rx.updated_at = datetime.now()

    db.query(PrescriptionItem).filter(
        PrescriptionItem.prescription_id == rx.id
    ).delete(synchronize_session=False)

    for raw_item in (payload.get("items") or []):
        if not isinstance(raw_item, dict):
            continue
        name = (raw_item.get("drug_name") or "").strip()
        if not name:
            continue
        db.add(PrescriptionItem(
            prescription_id = rx.id,
            drug_name       = name,
            dosage          = (raw_item.get("dosage") or "").strip() or None,
            frequency       = (raw_item.get("frequency") or "").strip() or None,
            duration        = (raw_item.get("duration") or "").strip() or None,
            instructions    = (raw_item.get("instructions") or "").strip() or None,
            notes           = (raw_item.get("notes") or "").strip() or None,
        ))

    db.commit()
    return JSONResponse({"ok": True, "saved_at": rx.updated_at.isoformat()})


# --------------------------------------------------------------------------- #
#  Delete — POST                                                               #
# --------------------------------------------------------------------------- #

@router.post("/prescriptions/{rx_id}/delete")
def prescription_delete(
    rx_id: int,
    request: Request,
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    rx = _get_prescription_or_404(rx_id, doctor.id, db)
    if not rx:
        return RedirectResponse(url="/patients", status_code=303)

    patient_id = rx.patient_id
    db.delete(rx)
    db.commit()
    return RedirectResponse(url=f"/patients/{patient_id}/prescriptions", status_code=303)


# --------------------------------------------------------------------------- #
#  Patient Prescription List — GET                                             #
# --------------------------------------------------------------------------- #

@router.get("/patients/{patient_id}/prescriptions", response_class=HTMLResponse)
def patient_prescriptions(
    patient_id: int,
    request: Request,
    doctor: Doctor = Depends(get_paying_doctor),
    db: Session = Depends(get_db),
):
    patient = db.query(Patient).filter(
        Patient.id == patient_id,
        Patient.doctor_id == doctor.id,
    ).first()
    if not patient:
        return RedirectResponse(url="/patients", status_code=303)

    prescriptions = (
        db.query(Prescription)
        .filter(
            Prescription.patient_id == patient_id,
            Prescription.doctor_id  == doctor.id,
        )
        .order_by(Prescription.created_at.desc())
        .all()
    )

    return templates.TemplateResponse(request, "prescription_list.html", {
        "doctor":        doctor,
        "patient":       patient,
        "prescriptions": prescriptions,
        "active":        "patients",
    })
