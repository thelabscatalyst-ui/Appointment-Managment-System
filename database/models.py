from datetime import datetime, date, time
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Date, Time,
    ForeignKey, Text, Enum as SAEnum, JSON, Numeric, UniqueConstraint
)
from sqlalchemy.orm import relationship
import enum

from database.connection import Base


# --------------------------------------------------------------------------- #
#  Enums                                                                        #
# --------------------------------------------------------------------------- #

class AppointmentStatus(str, enum.Enum):
    scheduled = "scheduled"
    completed = "completed"
    cancelled = "cancelled"
    no_show = "no_show"


class AppointmentType(str, enum.Enum):
    new_patient = "new_patient"
    follow_up = "follow_up"
    emergency = "emergency"


class PlanType(str, enum.Enum):
    trial      = "trial"
    solo       = "solo"       # 1 doctor   ₹599/mo
    duo        = "duo"        # 2 doctors  ₹699/mo
    clinic     = "clinic"     # 5 doctors  ₹1,599/mo
    hospital   = "hospital"   # 15 doctors ₹2,499/mo
    enterprise = "enterprise" # unlimited  ₹3,999/mo
    # legacy plans kept for existing subscribers
    basic = "basic"
    pro   = "pro"


class NotificationChannel(str, enum.Enum):
    whatsapp = "whatsapp"
    sms = "sms"


class NotificationType(str, enum.Enum):
    confirmation = "confirmation"
    reminder_24h = "reminder_24h"
    reminder_2h  = "reminder_2h"
    no_show      = "no_show"
    follow_up    = "follow_up"
    walkin_queue = "walkin_queue"
    bill_receipt = "bill_receipt"


class BookedBy(str, enum.Enum):
    doctor       = "doctor"
    patient      = "patient"
    staff_shared = "staff_shared"  # receptionist on shared login
    walk_in      = "walk_in"       # on-site patient, booked for now()
    staff        = "staff"         # Tier 2: dedicated staff account books for a doctor


class VisitStatus(str, enum.Enum):
    waiting         = "waiting"
    serving         = "serving"
    on_hold         = "on_hold"          # paused mid-consult (e.g. sent for x-ray); resumable
    billing_pending = "billing_pending"
    done            = "done"
    cancelled       = "cancelled"
    no_show         = "no_show"
    skipped         = "skipped"


class VisitSource(str, enum.Enum):
    walk_in     = "walk_in"
    appointment = "appointment"
    follow_up   = "follow_up"
    referral    = "referral"


class ReferralSource(str, enum.Enum):
    """How a patient first heard about the clinic — used for marketing analytics."""
    instagram       = "instagram"
    facebook        = "facebook"
    youtube         = "youtube"
    google          = "google"
    pamphlet        = "pamphlet"
    hoarding        = "hoarding"
    referral_friend = "referral_friend"
    walk_by         = "walk_by"
    other           = "other"


class PaymentMode(str, enum.Enum):
    cash      = "cash"
    upi       = "upi"
    card      = "card"
    insurance = "insurance"
    free      = "free"
    partial   = "partial"


class ExpenseCategory(str, enum.Enum):
    rent        = "rent"
    salaries    = "salaries"
    medicines   = "medicines"
    equipment   = "equipment"
    utilities   = "utilities"
    marketing   = "marketing"
    misc        = "misc"


# --------------------------------------------------------------------------- #
#  Clinic (Tier 2)                                                              #
# --------------------------------------------------------------------------- #

class Clinic(Base):
    __tablename__ = "clinics"

    id              = Column(Integer, primary_key=True, index=True)
    name            = Column(String(150), nullable=False)
    address         = Column(Text, nullable=True)
    city            = Column(String(100), nullable=True)
    slug            = Column(String(100), unique=True, index=True, nullable=True)
    plan_type       = Column(String(20), default="trial")   # trial | clinic
    plan_expires_at = Column(DateTime, nullable=True)
    owner_doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow)

    doctor_memberships = relationship("ClinicDoctor", back_populates="clinic", cascade="all, delete-orphan")


class ClinicDoctor(Base):
    """Junction table: doctor ↔ clinic (with role)."""
    __tablename__ = "clinic_doctors"

    id        = Column(Integer, primary_key=True, index=True)
    clinic_id = Column(Integer, ForeignKey("clinics.id"), nullable=False, index=True)
    doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=False, index=True)
    role      = Column(String(20), default="owner")   # owner | associate
    is_active = Column(Boolean, default=True)
    joined_at = Column(DateTime, default=datetime.utcnow)

    clinic = relationship("Clinic", back_populates="doctor_memberships")
    doctor = relationship("Doctor", back_populates="clinic_memberships")



class ClinicDoctorInvite(Base):
    """One-time invite for a doctor to join a clinic as associate."""
    __tablename__ = "clinic_doctor_invites"

    id         = Column(Integer, primary_key=True, index=True)
    clinic_id  = Column(Integer, ForeignKey("clinics.id"), nullable=False, index=True)
    email      = Column(String(200), nullable=False)
    token      = Column(String(100), unique=True, index=True, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    used_at    = Column(DateTime, nullable=True)

    clinic = relationship("Clinic")


class EmailVerification(Base):
    """One-time 6-digit code proving a doctor owns their email address.

    Follows the ClinicDoctorInvite pattern above, with three differences that
    matter for a credential:

      * `code_hash` — the code is stored hashed, never in plaintext, so DB
        read access does not hand over live verification codes.
      * `attempts`  — brute force over a 10^6 space is cheap, so we cap tries
        and invalidate the code once the cap is hit.
      * `consumed_at` — single use; a code cannot be replayed.

    Expiry is enforced by query predicate (expires_at > utcnow()), matching how
    ClinicDoctorInvite is queried elsewhere in the codebase.
    """
    __tablename__ = "email_verifications"

    id          = Column(Integer, primary_key=True, index=True)
    doctor_id   = Column(Integer, ForeignKey("doctors.id"), nullable=False, index=True)
    # The address this code was sent to. Kept separately from Doctor.email so a
    # code issued before a typo-correction can't verify the corrected address.
    email       = Column(String(150), nullable=False)
    code_hash   = Column(String(255), nullable=False)
    expires_at  = Column(DateTime, nullable=False)
    consumed_at = Column(DateTime, nullable=True)
    attempts    = Column(Integer, default=0, nullable=False)
    created_at  = Column(DateTime, default=datetime.utcnow)

    doctor = relationship("Doctor")


class PasswordReset(Base):
    """One-time link letting a doctor set a new password.

    A link rather than a 6-digit code: reset happens on whatever device holds
    the inbox, so a single click beats transcribing digits. (A code earns its
    place when the user is already in a session — the opposite of this case.)

    The token is generated with secrets.token_urlsafe(32) and stored HASHED;
    only the emailed URL carries the plaintext. A leaked database therefore
    cannot be used to seize accounts.
    """
    __tablename__ = "password_resets"

    id          = Column(Integer, primary_key=True, index=True)
    doctor_id   = Column(Integer, ForeignKey("doctors.id"), nullable=False, index=True)
    token_hash  = Column(String(255), nullable=False)
    expires_at  = Column(DateTime, nullable=False)
    consumed_at = Column(DateTime, nullable=True)
    # Recorded for audit — a reset is a security-sensitive event.
    requested_ip = Column(String(64), nullable=True)
    created_at  = Column(DateTime, default=datetime.utcnow)

    doctor = relationship("Doctor")


# --------------------------------------------------------------------------- #
#  Doctor                                                                       #
# --------------------------------------------------------------------------- #

class Doctor(Base):
    __tablename__ = "doctors"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    email = Column(String(150), unique=True, index=True, nullable=False)
    phone = Column(String(15), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    specialization = Column(String(100), nullable=True)
    clinic_name = Column(String(150), nullable=True)
    clinic_address = Column(Text, nullable=True)
    city = Column(String(100), nullable=True)
    languages = Column(String(200), nullable=True)  # comma-separated
    slug = Column(String(100), unique=True, index=True, nullable=True)  # for public booking URL
    pin_hash = Column(String(255), nullable=True)  # bcrypt PIN — protects billing/reports/settings
    is_active = Column(Boolean, default=True)
    plan_type = Column(SAEnum(PlanType), default=PlanType.trial)
    trial_ends_at = Column(DateTime, nullable=True)
    plan_expires_at = Column(DateTime, nullable=True)
    # v2 additions
    doctor_mode      = Column(String(30), default="reception_driven")  # reception_driven|phone_only|cabin_terminal
    walkin_policy    = Column(String(20), default="booked_jumps")      # booked_jumps|fcfs|ask
    avg_consult_mins = Column(Integer, default=10)                     # used for walk-in wait estimates
    plan_seats       = Column(Integer, nullable=True)                   # max doctors allowed under this plan (None = unlimited)
    # v3: medical registration
    medical_reg_number = Column(String(50), nullable=True)             # NMC/State council registration number
    is_verified        = Column(Boolean, default=False)                # MANUAL platform trust badge — NOT email verification
    # v4: email ownership verification.
    # Nullable timestamp rather than a boolean: it records *when*, which an
    # audit trail needs, and avoids the Postgres "BOOLEAN DEFAULT 0" trap that
    # silently drops the column (see database/connection.py:_add_column).
    email_verified_at  = Column(DateTime, nullable=True)
    # v4: bumped on password reset to invalidate every existing JWT. Without
    # this a stolen session would survive the very reset meant to kill it.
    # Carried in the token as "tv" and compared in get_current_doctor().
    token_version      = Column(Integer, default=0, nullable=False)
    # v5: platform operator — only admins can post the official answer that
    # closes a support query. Not a plan/clinic role; this is Med Track staff.
    is_admin           = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    appointments       = relationship("Appointment", back_populates="doctor", cascade="all, delete-orphan")
    patients           = relationship("Patient", back_populates="doctor", cascade="all, delete-orphan")
    schedules          = relationship("DoctorSchedule", back_populates="doctor", cascade="all, delete-orphan")
    blocked_dates      = relationship("BlockedDate", back_populates="doctor", cascade="all, delete-orphan")
    blocked_times      = relationship("BlockedTime", back_populates="doctor", cascade="all, delete-orphan")
    subscriptions      = relationship("Subscription", back_populates="doctor", cascade="all, delete-orphan")
    clinic_memberships = relationship("ClinicDoctor", back_populates="doctor")
    pinned_patients    = relationship("PinnedPatient", back_populates="doctor", cascade="all, delete-orphan")
    visits             = relationship("Visit", back_populates="doctor", cascade="all, delete-orphan")
    patient_documents  = relationship("PatientDocument", back_populates="doctor", cascade="all, delete-orphan")


# --------------------------------------------------------------------------- #
#  Patient                                                                      #
# --------------------------------------------------------------------------- #

class Patient(Base):
    __tablename__ = "patients"

    # ── PHI NOTE (DPDP Act compliance) ──────────────────────────────────────
    # The fields below (name, phone, age, gender, blood_group, allergies, notes)
    # constitute Protected Health Information (PHI) stored in plaintext.
    # Transport is encrypted via Railway TLS (HTTPS + Postgres TLS).
    # Field-level encryption (AES-256 via SQLAlchemy TypeDecorator + KMS) is
    # earmarked for a future release once a key-management strategy is in place.
    # ────────────────────────────────────────────────────────────────────────

    id = Column(Integer, primary_key=True, index=True)
    doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=False, index=True)
    clinic_id = Column(Integer, ForeignKey("clinics.id"), nullable=True, index=True)   # Phase 2
    name = Column(String(100), nullable=False)
    phone = Column(String(15), nullable=False)
    age = Column(Integer, nullable=True)
    gender = Column(String(10), nullable=True)        # male | female | other
    blood_group        = Column(String(10), nullable=True)
    allergies          = Column(Text, nullable=True)
    preferred_contact  = Column(String(20), nullable=True, default="phone")  # phone | whatsapp | none
    language_pref = Column(String(20), default="english")
    notes = Column(Text, nullable=True)
    # Marketing first-touch attribution — set only when the patient is created
    # via walk-in / appointment / public booking, then frozen unless edited from
    # the profile. Aggregated on the Reports page.
    referral_source       = Column(SAEnum(ReferralSource), nullable=True)
    referral_source_other = Column(String(120), nullable=True)
    visit_count = Column(Integer, default=0)
    first_visit = Column(Date, nullable=True)
    last_visit = Column(Date, nullable=True)
    # v3: WhatsApp consent — opt-out model: assumed granted by default for new
    # patients; the doctor unchecks it if the patient explicitly declines.
    wa_consent    = Column(Boolean, default=True)       # patient has consented to WhatsApp messages
    wa_consent_at = Column(DateTime, nullable=True)     # timestamp when consent was explicitly given
    created_at = Column(DateTime, default=datetime.utcnow)

    doctor       = relationship("Doctor", back_populates="patients")
    appointments = relationship("Appointment", back_populates="patient", cascade="all, delete-orphan")
    note_entries = relationship(
        "PatientNote", back_populates="patient",
        cascade="all, delete-orphan",
        order_by="PatientNote.created_at.desc()",
    )
    documents    = relationship("PatientDocument", back_populates="patient", cascade="all, delete-orphan")


# --------------------------------------------------------------------------- #
#  Patient Notes & File Attachments                                             #
# --------------------------------------------------------------------------- #

class PatientNote(Base):
    __tablename__ = "patient_notes"

    id         = Column(Integer, primary_key=True, index=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False, index=True)
    doctor_id  = Column(Integer, ForeignKey("doctors.id"), nullable=False, index=True)
    note_text  = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    patient = relationship("Patient", back_populates="note_entries")
    files   = relationship("NoteFile", back_populates="note", cascade="all, delete-orphan")


class NoteFile(Base):
    __tablename__ = "note_files"

    id            = Column(Integer, primary_key=True, index=True)
    note_id       = Column(Integer, ForeignKey("patient_notes.id"), nullable=False, index=True)
    original_name = Column(String(255), nullable=False)
    stored_name   = Column(String(255), nullable=False)
    file_size     = Column(Integer, nullable=True)   # bytes
    uploaded_at   = Column(DateTime, default=datetime.utcnow)

    note = relationship("PatientNote", back_populates="files")


# --------------------------------------------------------------------------- #
#  Patient Document Vault                                                       #
# --------------------------------------------------------------------------- #

DOCUMENT_CATEGORIES = {
    "invoice":           "Invoice / Bill",
    "lab_report":        "Lab Report",
    "prescription":      "Prescription",
    "xray_scan":         "X-Ray / Scan",
    "discharge_summary": "Discharge Summary",
    "insurance":         "Insurance",
    "other":             "Other",
}

class PatientDocument(Base):
    __tablename__ = "patient_documents"

    id            = Column(Integer, primary_key=True, index=True)
    doctor_id     = Column(Integer, ForeignKey("doctors.id"), nullable=False, index=True)
    patient_id    = Column(Integer, ForeignKey("patients.id"), nullable=False, index=True)
    original_name = Column(String(255), nullable=False)
    stored_name   = Column(String(255), nullable=False)
    file_size     = Column(Integer, nullable=False)      # bytes
    mime_type     = Column(String(100), nullable=True)
    category      = Column(String(50), default="other")  # lab_report | prescription | xray_scan | discharge_summary | insurance | other
    description   = Column(Text, nullable=True)
    uploaded_at   = Column(DateTime, default=datetime.utcnow)

    doctor  = relationship("Doctor",  back_populates="patient_documents")
    patient = relationship("Patient", back_populates="documents")


# --------------------------------------------------------------------------- #
#  Appointment                                                                  #
# --------------------------------------------------------------------------- #

class Appointment(Base):
    __tablename__ = "appointments"

    id = Column(Integer, primary_key=True, index=True)
    doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=False, index=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False, index=True)
    clinic_id = Column(Integer, ForeignKey("clinics.id"), nullable=True, index=True)   # Phase 2
    staff_id  = Column(Integer, nullable=True)                                          # legacy, unused
    appointment_date = Column(Date, nullable=False, index=True)
    appointment_time = Column(Time, nullable=False)
    duration_mins = Column(Integer, default=15)
    appointment_type = Column(SAEnum(AppointmentType), default=AppointmentType.follow_up)
    status = Column(SAEnum(AppointmentStatus), default=AppointmentStatus.scheduled, index=True)
    patient_notes = Column(Text, nullable=True)
    doctor_notes = Column(Text, nullable=True)
    reminder_24h_sent = Column(Boolean, default=False)
    reminder_2h_sent = Column(Boolean, default=False)
    booked_by = Column(SAEnum(BookedBy), default=BookedBy.staff_shared)
    is_emergency = Column(Boolean, default=False)   # bypasses quota/hours checks
    # v2: link to Visit once patient checks in
    visit_id         = Column(Integer, ForeignKey("visits.id"), nullable=True)
    arrival_status   = Column(String(20), nullable=True)
    reception_notes  = Column(Text, nullable=True)
    follow_up_date   = Column(Date, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    doctor = relationship("Doctor", back_populates="appointments")
    patient = relationship("Patient", back_populates="appointments")
    notifications = relationship("NotificationLog", back_populates="appointment", cascade="all, delete-orphan")


# --------------------------------------------------------------------------- #
#  Doctor Schedule                                                              #
# --------------------------------------------------------------------------- #

class DoctorSchedule(Base):
    __tablename__ = "doctor_schedules"

    id = Column(Integer, primary_key=True, index=True)
    doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=False, index=True)
    clinic_id = Column(Integer, ForeignKey("clinics.id"), nullable=True, index=True)   # Phase 2
    day_of_week = Column(Integer, nullable=False)  # 0=Monday … 6=Sunday
    start_time = Column(Time, nullable=False)
    end_time = Column(Time, nullable=False)
    slot_duration = Column(Integer, default=15)  # minutes
    max_patients = Column(Integer, default=30)
    walk_in_buffer = Column(Integer, default=0)  # slots reserved for walk-ins / emergencies
    is_active = Column(Boolean, default=True)

    doctor = relationship("Doctor", back_populates="schedules")


# --------------------------------------------------------------------------- #
#  Blocked Dates                                                                #
# --------------------------------------------------------------------------- #

class BlockedDate(Base):
    __tablename__ = "blocked_dates"

    id = Column(Integer, primary_key=True, index=True)
    doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=False, index=True)
    blocked_date = Column(Date, nullable=False)
    reason = Column(String(200), nullable=True)

    doctor = relationship("Doctor", back_populates="blocked_dates")


class BlockedTime(Base):
    """Block a specific time range on a date (e.g. 2–3 PM for an emergency)."""
    __tablename__ = "blocked_times"

    id         = Column(Integer, primary_key=True, index=True)
    doctor_id  = Column(Integer, ForeignKey("doctors.id"), nullable=False, index=True)
    blocked_date = Column(Date, nullable=False)
    start_time = Column(Time, nullable=False)
    end_time   = Column(Time, nullable=False)
    reason     = Column(String(200), nullable=True)

    doctor = relationship("Doctor", back_populates="blocked_times")


# --------------------------------------------------------------------------- #
#  Subscription                                                                 #
# --------------------------------------------------------------------------- #

class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=False, index=True)
    clinic_id = Column(Integer, ForeignKey("clinics.id"), nullable=True, index=True)   # Phase 2: clinic billing
    plan_name = Column(String(50), nullable=False)
    amount = Column(Integer, nullable=False)  # in paise (₹299 → 29900)
    payment_id = Column(String(100), nullable=True)  # Razorpay payment ID
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    status = Column(String(20), default="active")  # active, expired, failed

    doctor = relationship("Doctor", back_populates="subscriptions")


# --------------------------------------------------------------------------- #
#  Notification Log                                                             #
# --------------------------------------------------------------------------- #

class NotificationLog(Base):
    __tablename__ = "notifications_log"

    id = Column(Integer, primary_key=True, index=True)
    appointment_id = Column(Integer, ForeignKey("appointments.id"), nullable=True, index=True)
    type = Column(SAEnum(NotificationType), nullable=False)
    channel = Column(SAEnum(NotificationChannel), nullable=False)
    message_body = Column(Text, nullable=True)
    status = Column(String(20), default="pending")  # pending, sent, failed
    sent_at = Column(DateTime, nullable=True)

    appointment = relationship("Appointment", back_populates="notifications")


# --------------------------------------------------------------------------- #
#  PinnedPatient                                                                #
# --------------------------------------------------------------------------- #

class PinnedPatient(Base):
    __tablename__ = "pinned_patients"

    id         = Column(Integer, primary_key=True, index=True)
    doctor_id  = Column(Integer, ForeignKey("doctors.id"), nullable=False, index=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False)
    pinned_at  = Column(DateTime, default=datetime.utcnow, nullable=False)

    doctor  = relationship("Doctor", back_populates="pinned_patients")
    patient = relationship("Patient")


# --------------------------------------------------------------------------- #
#  Visit  (v2 — primary queue entity)                                          #
# --------------------------------------------------------------------------- #

class Visit(Base):
    __tablename__ = "visits"
    __table_args__ = (
        UniqueConstraint("doctor_id", "visit_date", "token_number",
                         name="uq_visit_token_per_doctor_day"),
    )

    id             = Column(Integer, primary_key=True, index=True)
    doctor_id      = Column(Integer, ForeignKey("doctors.id"), nullable=False, index=True)
    patient_id     = Column(Integer, ForeignKey("patients.id"), nullable=False, index=True)
    clinic_id      = Column(Integer, ForeignKey("clinics.id"), nullable=True, index=True)
    appointment_id = Column(Integer, ForeignKey("appointments.id"), nullable=True)

    visit_date     = Column(Date, nullable=False, index=True)
    token_number   = Column(Integer, nullable=False)       # monotonic per (doctor, date)
    queue_position = Column(Integer, nullable=True)        # mutable ordering hint

    status       = Column(SAEnum(VisitStatus), default=VisitStatus.waiting, nullable=False, index=True)
    is_emergency = Column(Boolean, default=False)
    source       = Column(SAEnum(VisitSource), default=VisitSource.walk_in)

    check_in_time  = Column(DateTime, nullable=True)
    call_time      = Column(DateTime, nullable=True)
    complete_time  = Column(DateTime, nullable=True)

    bill_id    = Column(Integer, nullable=True)   # set once bill is saved
    notes      = Column(Text, nullable=True)
    created_by = Column(Integer, nullable=True)   # staff/doctor id who checked in

    doctor      = relationship("Doctor", back_populates="visits")
    patient     = relationship("Patient")
    appointment = relationship("Appointment", primaryjoin="Visit.appointment_id == Appointment.id",
                               foreign_keys="[Visit.appointment_id]", uselist=False)
    bill        = relationship("Bill", back_populates="visit", uselist=False,
                               primaryjoin="Visit.id == Bill.visit_id",
                               foreign_keys="Bill.visit_id")


# --------------------------------------------------------------------------- #
#  Bill + BillItem  (v2 — generated when a Visit is closed)                    #
# --------------------------------------------------------------------------- #

class Bill(Base):
    __tablename__ = "bills"

    id         = Column(Integer, primary_key=True, index=True)
    visit_id   = Column(Integer, ForeignKey("visits.id"), unique=True, nullable=False, index=True)
    doctor_id  = Column(Integer, ForeignKey("doctors.id"), nullable=False, index=True)
    clinic_id  = Column(Integer, ForeignKey("clinics.id"), nullable=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False, index=True)

    subtotal   = Column(Numeric(10, 2), default=0)
    discount   = Column(Numeric(10, 2), default=0)
    gst_amount = Column(Numeric(10, 2), default=0)
    total      = Column(Numeric(10, 2), default=0)

    paid_amount  = Column(Numeric(10, 2), default=0)
    payment_mode = Column(SAEnum(PaymentMode), nullable=True)
    paid_at      = Column(DateTime, nullable=True)

    notes      = Column(Text, nullable=True)
    created_by = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    visit = relationship("Visit", back_populates="bill",
                         primaryjoin="Bill.visit_id == Visit.id",
                         foreign_keys="[Bill.visit_id]")
    items = relationship("BillItem", back_populates="bill", cascade="all, delete-orphan")


class BillItem(Base):
    __tablename__ = "bill_items"

    id          = Column(Integer, primary_key=True, index=True)
    bill_id     = Column(Integer, ForeignKey("bills.id"), nullable=False, index=True)
    description = Column(String(200), nullable=False)
    category    = Column(String(50), nullable=True)   # consultation|procedure|medicine|lab|other
    quantity    = Column(Integer, default=1)
    unit_price  = Column(Numeric(10, 2), nullable=False)
    total       = Column(Numeric(10, 2), nullable=False)
    gst_rate    = Column(Numeric(4, 2), default=0)

    bill = relationship("Bill", back_populates="items")


# --------------------------------------------------------------------------- #
#  PriceCatalog  (v2 — quick-add items for billing modal)                      #
# --------------------------------------------------------------------------- #

class PriceCatalog(Base):
    __tablename__ = "price_catalog"

    id            = Column(Integer, primary_key=True, index=True)
    doctor_id     = Column(Integer, ForeignKey("doctors.id"), nullable=False, index=True)
    clinic_id     = Column(Integer, ForeignKey("clinics.id"), nullable=True)
    name          = Column(String(100), nullable=False)
    category      = Column(String(50), nullable=True)
    default_price = Column(Numeric(10, 2), nullable=False)
    is_pinned     = Column(Boolean, default=False)   # show as quick button in bill modal
    sort_order    = Column(Integer, default=0)
    is_active     = Column(Boolean, default=True)
    created_at    = Column(DateTime, default=datetime.utcnow)


# --------------------------------------------------------------------------- #
#  Expense + RecurringExpense  (v2 — income dashboard)                         #
# --------------------------------------------------------------------------- #

class Expense(Base):
    __tablename__ = "expenses"

    id           = Column(Integer, primary_key=True, index=True)
    doctor_id    = Column(Integer, ForeignKey("doctors.id"), nullable=False, index=True)
    clinic_id    = Column(Integer, ForeignKey("clinics.id"), nullable=True)
    category     = Column(SAEnum(ExpenseCategory), nullable=False)
    amount       = Column(Numeric(10, 2), nullable=False)
    expense_date = Column(Date, nullable=False)
    description  = Column(String(300), nullable=True)
    recurring_id = Column(Integer, ForeignKey("recurring_expenses.id"), nullable=True)
    created_by   = Column(Integer, nullable=True)
    created_at   = Column(DateTime, default=datetime.utcnow)

    recurring = relationship("RecurringExpense", back_populates="expense_rows")


class RecurringExpense(Base):
    __tablename__ = "recurring_expenses"

    id           = Column(Integer, primary_key=True, index=True)
    doctor_id    = Column(Integer, ForeignKey("doctors.id"), nullable=False, index=True)
    clinic_id    = Column(Integer, ForeignKey("clinics.id"), nullable=True)
    category     = Column(SAEnum(ExpenseCategory), nullable=False)
    amount       = Column(Numeric(10, 2), nullable=False)
    label        = Column(String(100), nullable=False)
    day_of_month = Column(Integer, nullable=False)   # 1..28
    is_active    = Column(Boolean, default=True)
    created_at   = Column(DateTime, default=datetime.utcnow)

    expense_rows = relationship("Expense", back_populates="recurring")


# --------------------------------------------------------------------------- #
#  Prescription + PrescriptionItem  (e-prescription module)                    #
# --------------------------------------------------------------------------- #

class Prescription(Base):
    __tablename__ = "prescriptions"

    id         = Column(Integer, primary_key=True, index=True)
    doctor_id  = Column(Integer, ForeignKey("doctors.id"), nullable=False, index=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False, index=True)
    visit_id   = Column(Integer, ForeignKey("visits.id"), nullable=True, index=True)

    # Clinical content
    diagnosis  = Column(Text, nullable=True)
    advice     = Column(Text, nullable=True)     # general patient instructions
    follow_up  = Column(String(100), nullable=True)  # e.g. "After 7 days"

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # Relationships
    doctor  = relationship("Doctor")
    patient = relationship("Patient")
    visit   = relationship("Visit")
    items   = relationship(
        "PrescriptionItem",
        back_populates="prescription",
        cascade="all, delete-orphan",
        order_by="PrescriptionItem.id",
    )


class PrescriptionItem(Base):
    __tablename__ = "prescription_items"

    id              = Column(Integer, primary_key=True, index=True)
    prescription_id = Column(Integer, ForeignKey("prescriptions.id"), nullable=False, index=True)

    drug_name    = Column(String(150), nullable=False)
    dosage       = Column(String(80), nullable=True)    # "500mg", "10mg"
    frequency    = Column(String(80), nullable=True)    # "Twice daily", "SOS", "At bedtime"
    duration     = Column(String(60), nullable=True)    # "5 days", "2 weeks"
    instructions = Column(String(200), nullable=True)   # "After food", "With milk"
    notes        = Column(Text, nullable=True)          # optional free-text, per drug

    prescription = relationship("Prescription", back_populates="items")


class Feedback(Base):
    """Patient feedback / rating collected via a tokenized public link that is
    sent inside the WhatsApp bill receipt. One row is created per bill receipt;
    `rating` stays NULL until the patient actually submits the form."""
    __tablename__ = "feedback"

    id         = Column(Integer, primary_key=True, index=True)
    doctor_id  = Column(Integer, ForeignKey("doctors.id"),  nullable=False, index=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False, index=True)
    bill_id    = Column(Integer, ForeignKey("bills.id"),    nullable=True)

    token = Column(String(64), unique=True, nullable=False, index=True)  # public link id

    rating       = Column(Integer, nullable=True)   # 1–5 stars, NULL until submitted
    review       = Column(Text,    nullable=True)   # optional free-text review
    created_at   = Column(DateTime, default=datetime.utcnow)
    submitted_at = Column(DateTime, nullable=True)

    doctor  = relationship("Doctor")
    patient = relationship("Patient")


class SupportQuery(Base):
    """Public support board. Any doctor can post a query, visible to
    everyone — including logged-out visitors — the moment it's posted.
    Other doctors can reply while it's open ("chitchat"). Only an admin
    doctor can post the official closing answer; that reply is flagged
    is_official and locks the query against further replies, but the
    whole thread — including the official answer — stays visible forever."""
    __tablename__ = "support_queries"

    id        = Column(Integer, primary_key=True, index=True)
    doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=False, index=True)

    title = Column(String(200), nullable=False)
    body  = Column(Text, nullable=False)

    status     = Column(String(10), nullable=False, default="open")  # open|closed
    created_at = Column(DateTime, default=datetime.utcnow)
    closed_at  = Column(DateTime, nullable=True)

    doctor  = relationship("Doctor")
    replies = relationship(
        "SupportReply", back_populates="query",
        cascade="all, delete-orphan", order_by="SupportReply.created_at",
    )


class SupportReply(Base):
    __tablename__ = "support_replies"

    id        = Column(Integer, primary_key=True, index=True)
    query_id  = Column(Integer, ForeignKey("support_queries.id"), nullable=False, index=True)
    doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=False, index=True)

    body        = Column(Text, nullable=False)
    is_official = Column(Boolean, default=False)  # the admin's closing answer
    created_at  = Column(DateTime, default=datetime.utcnow)

    query  = relationship("SupportQuery", back_populates="replies")
    doctor = relationship("Doctor")
