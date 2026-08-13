"""
bill_pdf_service.py
Generates a styled PDF bill using fpdf2 (pure Python, no system deps).
Saves to patient vault automatically after every successful billing.
"""

import uuid
from datetime import datetime
from pathlib import Path

from fpdf import FPDF

from database.models import Doctor, Patient, Bill, PatientDocument, Appointment


# ── Warm parchment palette ─────────────────────────────────────────────────── #
BG       = (248, 242, 234)   # #f8f2ea  page background
CARD_BG  = (237, 231, 222)   # #ede7de  card fill
BORDER   = (194, 169, 138)   # #c2a98a  borders / dividers
TEXT     = ( 26,  20,  16)   # #1a1410  primary text
MUTED    = (107,  95,  85)   # #6b5f55  secondary text
DIM      = (154, 143, 133)   # #9a8f85  labels / captions
AMBER    = (180,  83,   9)   # discount row

# ── Layout constants ────────────────────────────────────────────────────────── #
CX   = 18    # left margin (mm)
CW   = 174   # content width  (210 − 18 − 18)
CR   = 192   # right edge     (CX + CW)
PX   = 6     # card horizontal inner padding
PY   = 6     # card vertical   inner padding
RH   = 7     # single-line row height

# Items table column widths — must sum to CW = 174
CD   = 90    # Description
CQ   = 14    # Qty
CP   = 35    # Unit Price
CT   = 35    # Total
assert CD + CQ + CP + CT == CW


# ── Helpers ────────────────────────────────────────────────────────────────── #

def _upload_dir(doctor_id: int, patient_id: int) -> Path:
    p = Path("uploads") / "patients" / str(doctor_id) / str(patient_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe(text) -> str:
    """Encode to Latin-1, replacing any character the Helvetica font can't render.
    Also normalises the Rs. rupee sign so ₹ never crashes the PDF builder."""
    if text is None:
        return ""
    s = str(text).replace("₹", "Rs.").replace("₹", "Rs.")
    return s.encode("latin-1", errors="replace").decode("latin-1")


def _fmt_inr(value) -> str:
    try:
        return f"Rs. {float(value):,.2f}"
    except (TypeError, ValueError):
        return "Rs. 0.00"


# ── Public entry points ────────────────────────────────────────────────────── #

def generate_and_store_bill_pdf(bill: Bill, db) -> None:
    """Failures are silently swallowed — billing must never be blocked."""
    import logging
    try:
        _do_generate(bill, db)
    except Exception as _e:
        logging.getLogger(__name__).error(f"bill_pdf failed for bill {bill.id}: {_e}", exc_info=True)


def regenerate_bill_pdf(bill: Bill, db) -> None:
    """Delete old vault PDF for this bill and generate a fresh one."""
    try:
        bill_id = bill.id
        _delete_existing_bill_pdf(bill, db)
        fresh = db.query(Bill).filter(Bill.id == bill_id).first()
        if fresh:
            _do_generate(fresh, db)
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"regenerate_bill_pdf failed for bill {bill.id}: {e}", exc_info=True)


def _delete_existing_bill_pdf(bill: Bill, db) -> None:
    upload_dir = _upload_dir(bill.doctor_id, bill.patient_id)
    prefix = f"bill_{bill.id}_"
    existing = db.query(PatientDocument).filter(
        PatientDocument.doctor_id  == bill.doctor_id,
        PatientDocument.patient_id == bill.patient_id,
        PatientDocument.stored_name.like(f"{prefix}%"),
    ).all()
    for doc in existing:
        try:
            (upload_dir / doc.stored_name).unlink(missing_ok=True)
        except Exception:
            pass
        db.delete(doc)
    db.commit()


def _do_generate(bill: Bill, db) -> None:
    """Legacy — vault storage removed. PDFs are now generated on-demand."""
    pass


# ── PDF class ──────────────────────────────────────────────────────────────── #

class BillPDF(FPDF):
    def __init__(self):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.set_margins(CX, 18, CX)
        self.set_auto_page_break(auto=True, margin=18)
        self.add_page()
        self.set_fill_color(*BG)
        self.rect(0, 0, 210, 297, "F")

    # ── Low-level primitives ─────────────────────────────────────────────── #

    def _c(self, *rgb):
        self.set_text_color(*rgb)

    def _divider(self, thickness: float = 0.3):
        self.set_draw_color(*BORDER)
        self.set_line_width(thickness)
        y = self.get_y()
        self.line(CX, y, CR, y)
        self.ln(3)

    def _section_label(self, text: str):
        self.set_font("Helvetica", "B", 8)
        self._c(*DIM)
        self.set_x(CX)
        self.cell(0, 5, text.upper(), ln=True)
        self.ln(1)

    def _card(self, x: float, y: float, w: float, h: float):
        """Filled, bordered rounded rectangle — matches app card style."""
        self.set_fill_color(*CARD_BG)
        self.set_draw_color(*BORDER)
        self.set_line_width(0.3)
        self.rect(x, y, w, h, style="FD", round_corners=True, corner_radius=3)

    def _count_lines(self, text: str, width: float) -> int:
        """Count wrapped lines for `text` in `width` mm at font size 11."""
        self.set_font("Helvetica", "", 11)
        words = str(text).split()
        if not words:
            return 1
        lines, line_w = 1, 0.0
        for word in words:
            w = self.get_string_width(word + " ")
            if line_w + w > width and line_w > 0:
                lines += 1
                line_w = w
            else:
                line_w += w
        return lines

    # ── Sections ─────────────────────────────────────────────────────────── #

    def header_block(self, doctor: Doctor, bill: Bill):
        """Clinic name + bill number on top row; sub-info on rows below."""
        # Row 1 — clinic name (left) + Bill #N (right)
        self.set_font("Helvetica", "B", 18)
        self._c(*TEXT)
        clinic = _safe(doctor.clinic_name or f"Dr. {doctor.name}")
        self.set_x(CX)
        self.cell(120, 10, clinic, ln=False)

        self.set_font("Helvetica", "B", 16)
        label = f"Bill #{bill.id}"
        lw    = self.get_string_width(label) + 4
        self.set_x(CR - lw)
        self.cell(lw, 10, label, ln=True, align="R")

        # Row 2 — doctor spec (left) + date (right)
        self.set_font("Helvetica", "", 10)
        self._c(*MUTED)
        spec = f"Dr. {doctor.name}"
        if doctor.specialization:
            spec += f"  ·  {doctor.specialization}"
        self.set_x(CX)
        self.cell(120, 5, _safe(spec), ln=False)

        date_val = bill.paid_at or bill.created_at
        date_str = date_val.strftime("%d %B %Y") if date_val else ""
        dw = self.get_string_width(date_str) + 4
        self.set_x(CR - dw)
        self.cell(dw, 5, date_str, ln=True, align="R")

        # Rows 3-4 — address, contact
        if doctor.clinic_address or doctor.city:
            addr = doctor.clinic_address or ""
            if doctor.city:
                addr = f"{addr}, {doctor.city}" if addr else doctor.city
            self.set_x(CX)
            self.cell(0, 5, _safe(addr), ln=True)

        if doctor.phone:
            contact = doctor.phone
            if doctor.email:
                contact += f"  ·  {doctor.email}"
            self.set_x(CX)
            self.cell(0, 5, _safe(contact), ln=True)

        self.ln(5)
        self._divider(thickness=0.6)
        self.ln(4)

    def patient_block(self, patient: Patient, visit, appt, bill: Bill):
        self._section_label("Patient")

        R1     = 8    # name row height
        R2     = 6    # meta row height
        card_h = PY + R1 + R2 + PY

        card_y = self.get_y()
        self._card(CX, card_y, CW, card_h)

        # Name (left) + token / date (right)
        self.set_y(card_y + PY)
        self.set_x(CX + PX)
        self.set_font("Helvetica", "B", 13)
        self._c(*TEXT)
        self.cell(110, R1, _safe(patient.name), ln=False)

        if visit:
            self.set_font("Helvetica", "", 10)
            self._c(*MUTED)
            visit_date_str = visit.visit_date.strftime('%d %b %Y') if visit.visit_date else ""
            vi = f"Token #{visit.token_number}  ·  {visit_date_str}"
            vw = self.get_string_width(vi) + 2
            self.set_x(CR - PX - vw)
            self.cell(vw, R1, vi, ln=True, align="R")
        else:
            self.ln(R1)

        # Meta (left) + appt type (right)
        self.set_x(CX + PX)
        self.set_font("Helvetica", "", 10)
        self._c(*MUTED)
        parts = [patient.phone or ""]
        if patient.age:          parts.append(f"{patient.age} yrs")
        if patient.gender:       parts.append(patient.gender.title())
        if patient.blood_group:  parts.append(patient.blood_group)
        meta = "  ·  ".join(p for p in parts if p)
        self.cell(110, R2, _safe(meta), ln=False)

        if appt:
            atype = appt.appointment_type.value.replace("_", " ").title()
            aw = self.get_string_width(atype) + 2
            self.set_x(CR - PX - aw)
            self.cell(aw, R2, atype, ln=True, align="R")
        else:
            self.ln(R2)

        self.set_y(card_y + card_h + 6)

    def items_table(self, items, bill: Bill):
        self._section_label("Items")

        rows       = items if items else []
        desc_avail = CD - PX   # text width inside description column

        # ── Pre-calculate row heights for multi-line descriptions ──
        if rows:
            row_heights = [
                max(RH, self._count_lines(_safe(item.description), desc_avail) * RH)
                for item in rows
            ]
        else:
            row_heights = [RH]

        # card = top padding + header row + divider gap + data rows + bottom padding
        card_h = PY + RH + 4 + sum(row_heights) + PY
        card_y = self.get_y()
        self._card(CX, card_y, CW, card_h)

        # ── Header row ──
        self.set_y(card_y + PY)
        self.set_x(CX + PX)
        self.set_font("Helvetica", "B", 9)
        self._c(*MUTED)
        self.cell(CD - PX, RH, "Description",  ln=False, align="L")
        self.cell(CQ,      RH, "Qty",           ln=False, align="C")
        self.cell(CP,      RH, "Unit Price",    ln=False, align="R")
        self.cell(CT - PX, RH, "Total",         ln=True,  align="R")

        self._divider(thickness=0.25)

        # ── Data rows ──
        if not rows:
            # Fallback: single "Consultation" row
            self.set_x(CX + PX)
            self.set_font("Helvetica", "", 11)
            self._c(*TEXT)
            self.cell(CD - PX, RH, "Consultation",          ln=False)
            self._c(*MUTED)
            self.set_font("Helvetica", "", 10)
            self.cell(CQ,      RH, "1",                     ln=False, align="C")
            self._c(*TEXT)
            self.set_font("Helvetica", "", 11)
            self.cell(CP,      RH, _fmt_inr(bill.subtotal), ln=False, align="R")
            self.cell(CT - PX, RH, _fmt_inr(bill.subtotal), ln=True,  align="R")
        else:
            for item, rh in zip(rows, row_heights):
                row_y = self.get_y()

                # Description — wraps automatically via multi_cell
                self.set_xy(CX + PX, row_y)
                self.set_font("Helvetica", "", 11)
                self._c(*TEXT)
                self.multi_cell(CD - PX, RH, _safe(item.description), align="L")

                # Qty / Unit Price / Total — anchored to row_y, full rh height
                self.set_xy(CX + CD, row_y)
                self._c(*MUTED)
                self.set_font("Helvetica", "", 10)
                self.cell(CQ,      rh, str(item.quantity),        ln=False, align="C")
                self._c(*TEXT)
                self.set_font("Helvetica", "", 11)
                self.cell(CP,      rh, _fmt_inr(item.unit_price), ln=False, align="R")
                self.cell(CT - PX, rh, _fmt_inr(item.total),     ln=True,  align="R")

                self.set_y(row_y + rh)

        self.set_y(card_y + card_h + 5)
        self._divider()

    def totals_block(self, bill: Bill):
        self.ln(2)

        disc          = float(bill.discount   or 0)
        gst           = float(bill.gst_amount or 0)
        show_subtotal = disc > 0 or gst > 0
        n_detail      = (1 if show_subtotal else 0) + (1 if disc > 0 else 0) + (1 if gst > 0 else 0)

        # Height: detail rows  +  divider gap (8mm when present)  +  total row
        DIVIDER_GAP = 8
        card_h = PY + (n_detail * RH) + (DIVIDER_GAP if show_subtotal else 0) + RH + PY

        card_y = self.get_y()
        self._card(CX, card_y, CW, card_h)
        self.set_y(card_y + PY)

        col_label = CW - 52   # label column (right-aligned text)
        col_value = 52 - PX   # value column (leaves PX breathing room on right)

        def trow(label: str, value: str, bold: bool = False, color=MUTED):
            self.set_x(CX)
            fs = 11 if bold else 10
            self.set_font("Helvetica", "B" if bold else "", fs)
            self._c(*color)
            self.cell(col_label, RH, label, ln=False, align="R")
            self.cell(col_value, RH, value, ln=True,  align="R")

        if show_subtotal:
            trow("Subtotal", _fmt_inr(bill.subtotal))
        if disc > 0:
            trow("Discount", f"- {_fmt_inr(disc)}", color=AMBER)
        if gst > 0:
            trow("GST", _fmt_inr(gst))

        if show_subtotal:
            self.ln(2)
            self._divider(thickness=0.4)
            self.ln(1)
        else:
            # Slim spacer before the lone Total row
            self.ln(3)

        trow("Total", _fmt_inr(bill.total), bold=True, color=TEXT)
        self.set_y(card_y + card_h + 5)

    def payment_block(self, bill: Bill):
        if not bill.payment_mode:
            return

        card_h = PY + RH + PY
        card_y = self.get_y()
        self._card(CX, card_y, CW, card_h)

        self.set_y(card_y + PY)
        self.set_x(CX + PX)

        self.set_font("Helvetica", "", 10)
        self._c(*MUTED)
        self.cell(22, RH, "Paid via", ln=False)

        self.set_font("Helvetica", "B", 11)
        self._c(*TEXT)
        self.cell(38, RH, bill.payment_mode.value.upper(), ln=False)

        if bill.paid_at:
            paid_str = f"on {bill.paid_at.strftime('%d %b %Y, %I:%M %p')}"
            self.set_font("Helvetica", "", 9)
            self._c(*MUTED)
            pw = self.get_string_width(paid_str) + 2
            self.set_x(CR - PX - pw)
            self.cell(pw, RH, paid_str, ln=True, align="R")
        else:
            self.ln(RH)

        self.set_y(card_y + card_h + 4)

        if bill.notes:
            self.ln(2)
            self.set_x(CX)
            self.set_font("Helvetica", "", 9)
            self._c(*MUTED)
            self.multi_cell(CW, 5, _safe(f"Note: {bill.notes}"))

        self.ln(8)

    def footer_block(self, doctor: Doctor):
        self._divider()
        self.ln(2)

        clinic = doctor.clinic_name or f"Dr. {doctor.name}"
        self.set_font("Helvetica", "I", 10)
        self._c(*MUTED)
        self.set_x(CX)
        self.cell(120, 5, _safe(f"Thank you for visiting {clinic}"), ln=False)

        gen_str = f"Generated by Nivora  ·  {datetime.now().strftime('%d %b %Y')}"
        self.set_font("Helvetica", "", 8)
        self._c(*DIM)
        gw = self.get_string_width(gen_str) + 2
        self.set_x(CR - gw)
        self.cell(gw, 5, gen_str, ln=True, align="R")


# ── Entry point ────────────────────────────────────────────────────────────── #

def _build_pdf(bill, patient, doctor, visit, appt, items) -> BillPDF:
    pdf = BillPDF()
    pdf.header_block(doctor, bill)
    pdf.patient_block(patient, visit, appt, bill)
    pdf.items_table(items, bill)
    pdf.totals_block(bill)
    pdf.payment_block(bill)
    pdf.footer_block(doctor)
    return pdf
