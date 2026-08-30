"""
routers/billing_ops.py — Visit billing + price catalog

Routes:
  GET  /visits/{id}/bill-prefill    JSON prefill data for the bill modal
  POST /visits/{id}/bill            Create bill + close visit
  GET  /bills/{id}                  Bill detail page
  GET  /price-catalog               JSON list of catalog items for doctor
  POST /price-catalog               Add a catalog item
  POST /price-catalog/{id}/delete   Remove a catalog item
  POST /price-catalog/{id}/pin      Toggle pinned (quick button in modal)
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Request, Depends, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database.connection import get_db
from database.models import (
    Doctor, Patient, Visit, VisitStatus, Bill, BillItem,
    PriceCatalog, PaymentMode, ClinicDoctor, Appointment,
)
from services.auth_service import get_paying_doctor, require_clinic_owner_context
import services.visit_service as vs

router = APIRouter(tags=["billing_ops"])
templates = Jinja2Templates(directory="templates")


# ── helpers ──────────────────────────────────────────────────────────────── #

def _get_primary_clinic(doctor: Doctor, db: Session, request=None):
    """The clinic this work should be attributed to.

    Was an arbitrary `.first()` active membership with no role filter and no
    ORDER BY — for a two-clinic doctor it returned whichever row the database
    yielded first, so every visit and bill they ever created was stamped with
    that one clinic regardless of where the work happened. Now it follows the
    clinic the request is actually operating in.
    """
    from services.clinic_context import active_memberships
    from database.models import Clinic

    if request is not None:
        clinic = getattr(request.state, "active_clinic", None)
        if clinic is not None:
            return clinic
        cid = getattr(request.state, "active_clinic_id", None)
        if cid:
            return db.query(Clinic).filter(Clinic.id == cid).first()

    ms = active_memberships(db, doctor.id)   # owner-first, deterministic
    return ms[0].clinic if ms else None


def _get_visit(visit_id: int, doctor_id: int, db: Session) -> Optional[Visit]:
    return db.query(Visit).filter(
        Visit.id == visit_id,
        Visit.doctor_id == doctor_id,
    ).first()


def _auto_complete_appointment(db: Session, visit: Visit):
    from database.models import AppointmentStatus
    if visit.appointment_id:
        appt = db.query(Appointment).filter(Appointment.id == visit.appointment_id).first()
        if appt and appt.status == AppointmentStatus.scheduled:
            appt.status = AppointmentStatus.completed


# ── Bill prefill JSON ─────────────────────────────────────────────────────── #

@router.get("/visits/{visit_id}/bill-prefill", response_class=JSONResponse)
async def bill_prefill(
    visit_id: int,
    db: Session    = Depends(get_db),
    doctor: Doctor = Depends(get_paying_doctor),
):
    visit = _get_visit(visit_id, doctor.id, db)
    if not visit:
        return JSONResponse({"error": "not found"}, status_code=404)

    # Pinned catalog items as quick buttons
    pinned = (
        db.query(PriceCatalog)
        .filter(
            PriceCatalog.doctor_id == doctor.id,
            PriceCatalog.is_active == True,
            PriceCatalog.is_pinned == True,
        )
        .order_by(PriceCatalog.sort_order, PriceCatalog.name)
        .all()
    )

    # All active catalog items for the dropdown
    all_items = (
        db.query(PriceCatalog)
        .filter(
            PriceCatalog.doctor_id == doctor.id,
            PriceCatalog.is_active == True,
        )
        .order_by(PriceCatalog.sort_order, PriceCatalog.name)
        .all()
    )

    # Guess default fee from appointment type if available
    default_fee = 0.0
    if visit.appointment_id:
        appt = db.query(Appointment).filter(Appointment.id == visit.appointment_id).first()
        if appt:
            # Try to find a matching catalog item by appointment type name
            type_name = appt.appointment_type.value.replace("_", " ").title()
            match = next((i for i in all_items if type_name.lower() in i.name.lower()), None)
            if match:
                default_fee = float(match.default_price)

    # Return existing bill data if one already exists for this visit
    existing_bill = None
    if visit.bill_id:
        bill_obj = db.query(Bill).filter(
            Bill.id == visit.bill_id,
            Bill.doctor_id == doctor.id,
        ).first()
        if bill_obj:
            existing_bill = {
                "id":           bill_obj.id,
                "items": [
                    {
                        "description": it.description,
                        "quantity":    it.quantity,
                        "unit_price":  float(it.unit_price),
                    }
                    for it in bill_obj.items
                ],
                "subtotal":     float(bill_obj.subtotal),
                "discount":     float(bill_obj.discount),
                "gst_amount":   float(bill_obj.gst_amount),
                "total":        float(bill_obj.total),
                "paid_amount":  float(bill_obj.paid_amount),
                "payment_mode": bill_obj.payment_mode.value if bill_obj.payment_mode else None,
                "notes":        bill_obj.notes,
            }

    return JSONResponse({
        "visit_id":     visit.id,
        "patient_name": visit.patient.name,
        "default_fee":  default_fee,
        "pinned":  [{"id": i.id, "name": i.name, "price": float(i.default_price)} for i in pinned],
        "catalog": [{"id": i.id, "name": i.name, "price": float(i.default_price)} for i in all_items],
        "existing_bill": existing_bill,
    })


# ── Create bill + close visit ─────────────────────────────────────────────── #

@router.post("/visits/{visit_id}/bill")
async def create_bill(
    visit_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session    = Depends(get_db),
    doctor: Doctor = Depends(get_paying_doctor),
):
    form = await request.form()

    visit = _get_visit(visit_id, doctor.id, db)
    if not visit:
        return RedirectResponse("/appointments", status_code=303)

    # Refuse edits on already-terminal visits — those go through /bills/{id}/edit
    _terminal = {VisitStatus.done, VisitStatus.cancelled, VisitStatus.no_show}
    if visit.status in _terminal:
        return RedirectResponse("/appointments", status_code=303)

    # Determine action: "save" (persist only) or "close" (finalize visit). Default to "close"
    # so that the existing one-button modal path continues to work unchanged.
    action = (form.get("action") or "close").strip().lower()
    if action not in ("save", "close"):
        action = "close"

    primary_clinic = _get_primary_clinic(doctor, db, request)

    # ── Parse numeric fields ──────────────────────────────────────────────── #
    try:
        fee = float(form.get("fee") or 0)
    except (ValueError, TypeError):
        fee = 0.0
    try:
        discount = float(form.get("discount") or 0)
    except (ValueError, TypeError):
        discount = 0.0
    try:
        gst_amount = float(form.get("gst_amount") or 0)
    except (ValueError, TypeError):
        gst_amount = 0.0

    payment_mode_str = form.get("payment_mode", "cash")
    notes = (form.get("notes") or "").strip()

    # ── Parse line items — item_name[], item_price[], item_qty[] ────────────── #
    item_names      = form.getlist("item_name")
    item_prices_raw = form.getlist("item_price")
    item_qtys_raw   = form.getlist("item_qty")

    items = []
    items_subtotal = 0.0
    for i, (name, price_raw) in enumerate(zip(item_names, item_prices_raw)):
        name = name.strip()
        if not name:
            continue
        try:
            price = float(price_raw)
        except (ValueError, TypeError):
            price = 0.0
        try:
            qty = max(1, int(float(item_qtys_raw[i]))) if i < len(item_qtys_raw) else 1
        except (ValueError, TypeError):
            qty = 1
        line_total = price * qty
        items.append((name, qty, price, line_total))
        items_subtotal += line_total

    subtotal = items_subtotal if items else fee
    disc     = min(discount, subtotal)
    total    = max(0.0, subtotal - disc + gst_amount)

    # ── Parse paid_amount (new field; defaults to total for legacy full-pay) ── #
    paid_amount_raw = form.get("paid_amount")
    if paid_amount_raw is not None and str(paid_amount_raw).strip() != "":
        try:
            paid_amount = float(paid_amount_raw)
        except (ValueError, TypeError):
            paid_amount = total
    else:
        paid_amount = total  # legacy default: full payment
    # Clamp to [0, total]
    paid_amount = max(0.0, min(paid_amount, total))

    # ── Derive payment mode and paid_at from paid_amount ──────────────────── #
    try:
        mode = PaymentMode(payment_mode_str)
    except ValueError:
        mode = PaymentMode.cash

    if paid_amount == 0.0:
        # Nothing collected yet — preserve selected mode, no paid_at timestamp
        paid_at = None
    elif paid_amount < total:
        # Partial payment — force mode to partial regardless of selection
        mode    = PaymentMode.partial
        paid_at = datetime.now()
    else:
        # Full payment (paid_amount == total after clamping)
        paid_at = datetime.now()

    # ── Upsert bill ───────────────────────────────────────────────────────── #
    if visit.bill_id:
        # Update existing bill in-place
        bill = db.query(Bill).filter(
            Bill.id == visit.bill_id,
            Bill.doctor_id == doctor.id,
        ).first()
        if not bill:
            # Defensive: bill_id set but row missing — treat as new bill
            bill = None

        if bill:
            # Delete existing items and replace
            for old_item in list(bill.items):
                db.delete(old_item)
            db.flush()

            bill.subtotal     = subtotal
            bill.discount     = disc
            bill.gst_amount   = gst_amount
            bill.total        = total
            bill.paid_amount  = paid_amount
            bill.payment_mode = mode
            bill.paid_at      = paid_at
            bill.notes        = notes or None
        else:
            visit.bill_id = None  # reset so the create path runs below

    if not visit.bill_id:
        # Create new bill
        bill = Bill(
            visit_id     = visit.id,
            doctor_id    = doctor.id,
            clinic_id    = primary_clinic.id if primary_clinic else None,
            patient_id   = visit.patient_id,
            subtotal     = subtotal,
            discount     = disc,
            gst_amount   = gst_amount,
            total        = total,
            paid_amount  = paid_amount,
            payment_mode = mode,
            paid_at      = paid_at,
            notes        = notes or None,
            created_by   = doctor.id,
        )
        db.add(bill)
        db.flush()
        # Link the new bill back to the visit immediately so it survives a save
        visit.bill_id = bill.id

    # Add (or re-add) line items
    for name, qty, unit_price, line_total in items:
        db.add(BillItem(
            bill_id     = bill.id,
            description = name,
            quantity    = qty,
            unit_price  = unit_price,
            total       = line_total,
        ))

    # ── Branch on action ─────────────────────────────────────────────────── #
    if action == "save":
        # Persist bill; leave visit.status untouched; no WhatsApp
        db.commit()
        return RedirectResponse("/appointments", status_code=303)

    # action == "close"
    _auto_complete_appointment(db, visit)
    vs.close_visit(db, visit, bill.id)

    # PDF is generated on-demand at download time — no vault storage needed
    # WhatsApp receipt runs after the response so it never blocks the click.
    from services.notification_service import send_bill_receipt_bg
    background_tasks.add_task(send_bill_receipt_bg, bill.id, doctor.id)

    return RedirectResponse("/appointments", status_code=303)


# ── Edit bill ────────────────────────────────────────────────────────────── #

@router.get("/bills/{bill_id}/edit", response_class=HTMLResponse)
async def edit_bill_page(
    bill_id: int,
    request: Request,
    db: Session    = Depends(get_db),
    doctor: Doctor = Depends(get_paying_doctor),
):
    bill = db.query(Bill).filter(
        Bill.id == bill_id,
        Bill.doctor_id == doctor.id,
    ).first()
    if not bill:
        return RedirectResponse("/appointments", status_code=303)

    price_catalog = (
        db.query(PriceCatalog)
        .filter(PriceCatalog.doctor_id == doctor.id, PriceCatalog.is_active == True)
        .order_by(PriceCatalog.sort_order, PriceCatalog.name)
        .all()
    )

    return templates.TemplateResponse(request, "bill_edit.html", {
        "active":        "appointments",
        "doctor":        doctor,
        "bill":          bill,
        "price_catalog": price_catalog,
    })


@router.post("/bills/{bill_id}/edit")
async def edit_bill(
    bill_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session    = Depends(get_db),
    doctor: Doctor = Depends(get_paying_doctor),
):
    bill = db.query(Bill).filter(
        Bill.id == bill_id,
        Bill.doctor_id == doctor.id,
    ).first()
    if not bill:
        return RedirectResponse("/appointments", status_code=303)

    form = await request.form()

    try:
        discount = float(form.get("discount") or 0)
    except ValueError:
        discount = 0.0
    try:
        gst_amount = float(form.get("gst_amount") or 0)
    except ValueError:
        gst_amount = 0.0

    payment_mode_str = form.get("payment_mode", "cash")
    notes = (form.get("notes") or "").strip()

    item_names      = form.getlist("item_name")
    item_prices_raw = form.getlist("item_price")
    item_qtys_raw   = form.getlist("item_qty")

    items = []
    items_subtotal = 0.0
    for i, (name, price_raw) in enumerate(zip(item_names, item_prices_raw)):
        name = name.strip()
        if not name:
            continue
        try:
            price = float(price_raw)
        except (ValueError, TypeError):
            price = 0.0
        try:
            qty = max(1, int(float(item_qtys_raw[i]))) if i < len(item_qtys_raw) else 1
        except (ValueError, TypeError):
            qty = 1
        line_total = price * qty
        items.append((name, qty, price, line_total))
        items_subtotal += line_total

    subtotal = items_subtotal if items else float(bill.subtotal)
    disc     = min(discount, subtotal)
    total    = max(0.0, subtotal - disc + gst_amount)

    try:
        mode = PaymentMode(payment_mode_str)
    except ValueError:
        mode = PaymentMode.cash

    bill.subtotal     = subtotal
    bill.discount     = disc
    bill.gst_amount   = gst_amount
    bill.total        = total
    bill.paid_amount  = total
    bill.payment_mode = mode
    bill.notes        = notes or None

    # Replace all items
    for item in list(bill.items):
        db.delete(item)
    db.flush()
    for name, qty, unit_price, line_total in items:
        db.add(BillItem(
            bill_id     = bill.id,
            description = name,
            quantity    = qty,
            unit_price  = unit_price,
            total       = line_total,
        ))

    db.commit()

    # PDF is generated on-demand at download time — no vault storage needed
    # WhatsApp receipt runs after the response so it never blocks the click.
    from services.notification_service import send_bill_receipt_bg
    background_tasks.add_task(send_bill_receipt_bg, bill.id, doctor.id)

    return RedirectResponse(f"/patients/{bill.patient_id}", status_code=303)


# ── Mark bill as paid (from pending-collections) ─────────────────────────── #

@router.post("/bills/{bill_id}/mark-paid")
async def mark_bill_paid(
    bill_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session    = Depends(get_db),
    doctor: Doctor = Depends(get_paying_doctor),
):
    bill = db.query(Bill).filter(
        Bill.id == bill_id,
        Bill.doctor_id == doctor.id,
    ).first()
    if bill and bill.paid_amount == 0:
        bill.paid_amount  = bill.total
        bill.payment_mode = bill.payment_mode or PaymentMode.cash
        bill.paid_at      = datetime.now()
        db.commit()
        from services.notification_service import send_bill_receipt_bg
        background_tasks.add_task(send_bill_receipt_bg, bill.id, doctor.id)
    return RedirectResponse("/income", status_code=303)


# ── Bill detail page ──────────────────────────────────────────────────────── #

@router.get("/bills/{bill_id}", response_class=HTMLResponse)
async def bill_detail(
    bill_id: int,
    request: Request,
    db: Session    = Depends(get_db),
    doctor: Doctor = Depends(get_paying_doctor),
):
    bill = db.query(Bill).filter(
        Bill.id == bill_id,
        Bill.doctor_id == doctor.id,
    ).first()
    if not bill:
        return RedirectResponse("/appointments", status_code=303)

    return templates.TemplateResponse(request, "bill_detail.html", {
        "active": "appointments",
        "doctor": doctor,
        "bill":   bill,
    })


# ── Bill PDF download ─────────────────────────────────────────────────────── #

@router.get("/bills/{bill_id}/pdf")
async def download_bill_pdf(
    bill_id: int,
    db: Session    = Depends(get_db),
    doctor: Doctor = Depends(get_paying_doctor),
):
    bill = db.query(Bill).filter(
        Bill.id == bill_id,
        Bill.doctor_id == doctor.id,
    ).first()
    if not bill:
        return RedirectResponse("/appointments", status_code=303)

    try:
        from services.bill_pdf_service import _build_pdf
        patient = db.query(Patient).filter(
            Patient.id == bill.patient_id,
            Patient.doctor_id == doctor.id,
        ).first()
        visit   = bill.visit
        appt    = None
        if visit and visit.appointment_id:
            appt = db.query(Appointment).filter(Appointment.id == visit.appointment_id).first()
        items   = list(bill.items)
        pdf     = _build_pdf(bill, patient, doctor, visit, appt, items)
        data    = bytes(pdf.output())
        fname   = f"bill_{bill.id}_{patient.name.replace(' ','_') if patient else 'receipt'}.pdf"
        return Response(
            content=data,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )
    except Exception:
        return RedirectResponse(f"/bills/{bill_id}", status_code=303)


# ── Price catalog CRUD ────────────────────────────────────────────────────── #

@router.get("/price-catalog", response_class=JSONResponse)
async def get_catalog(
    db: Session    = Depends(get_db),
    doctor: Doctor = Depends(require_clinic_owner_context),
):
    items = (
        db.query(PriceCatalog)
        .filter(PriceCatalog.doctor_id == doctor.id, PriceCatalog.is_active == True)
        .order_by(PriceCatalog.sort_order, PriceCatalog.name)
        .all()
    )
    return [{"id": i.id, "name": i.name, "price": float(i.default_price), "pinned": i.is_pinned} for i in items]


@router.post("/price-catalog")
async def add_catalog_item(
    request: Request,
    name: str   = Form(...),
    price: float = Form(...),
    pinned: bool = Form(False),
    db: Session  = Depends(get_db),
    doctor: Doctor = Depends(require_clinic_owner_context),
):
    item = PriceCatalog(
        doctor_id     = doctor.id,
        name          = name.strip(),
        default_price = price,
        is_pinned     = pinned,
        is_active     = True,
    )
    db.add(item)
    db.commit()
    return RedirectResponse("/doctors/settings?tab=catalog", status_code=303)


@router.post("/price-catalog/{item_id}/delete")
async def delete_catalog_item(
    item_id: int,
    request: Request,
    db: Session    = Depends(get_db),
    doctor: Doctor = Depends(require_clinic_owner_context),
):
    item = db.query(PriceCatalog).filter(
        PriceCatalog.id == item_id,
        PriceCatalog.doctor_id == doctor.id,
    ).first()
    if item:
        item.is_active = False
        db.commit()
    return RedirectResponse("/doctors/settings?tab=catalog", status_code=303)


@router.post("/price-catalog/{item_id}/pin")
async def toggle_pin(
    item_id: int,
    request: Request,
    db: Session    = Depends(get_db),
    doctor: Doctor = Depends(require_clinic_owner_context),
):
    item = db.query(PriceCatalog).filter(
        PriceCatalog.id == item_id,
        PriceCatalog.doctor_id == doctor.id,
    ).first()
    if item:
        item.is_pinned = not item.is_pinned
        db.commit()
    return RedirectResponse("/doctors/settings?tab=catalog", status_code=303)
