from datetime import datetime, date, time
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Date, Time,
    ForeignKey, Text, Enum as SAEnum, JSON
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
    trial = "trial"
    solo  = "solo"    # ₹399 — Tier 1 Solo plan
    basic = "basic"   # legacy ₹299 (existing subscribers)
    pro   = "pro"     # legacy ₹499 (existing subscribers)


class NotificationChannel(str, enum.Enum):
    whatsapp = "whatsapp"
    sms = "sms"


class NotificationType(str, enum.Enum):
    confirmation = "confirmation"
    reminder_24h = "reminder_24h"
    reminder_2h = "reminder_2h"
    no_show = "no_show"
    follow_up = "follow_up"


class BookedBy(str, enum.Enum):
    doctor       = "doctor"
    patient      = "patient"
    staff_shared = "staff_shared"  # receptionist on shared login
    walk_in      = "walk_in"       # on-site patient, booked for now()
    staff        = "staff"         # Tier 2: dedicated staff account books for a doctor


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
    staff              = relationship("Staff", back_populates="clinic", cascade="all, delete-orphan")
    invites            = relationship("StaffInvite", back_populates="clinic", cascade="all, delete-orphan")


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


class Staff(Base):
    """Receptionist / manager with their own login, scoped to a clinic."""
    __tablename__ = "staff"

    id                 = Column(Integer, primary_key=True, index=True)
    clinic_id          = Column(Integer, ForeignKey("clinics.id"), nullable=False, index=True)
    name               = Column(String(100), nullable=False)
    email              = Column(String(150), unique=True, index=True, nullable=False)
    password_hash      = Column(String(255), nullable=True)  # null until invite accepted
    role               = Column(String(20), default="receptionist")  # receptionist | manager
    allowed_doctor_ids = Column(JSON, default=list)   # [] = all doctors in clinic
    is_active          = Column(Boolean, default=True)
    created_at         = Column(DateTime, default=datetime.utcnow)

    clinic = relationship("Clinic", back_populates="staff")


class StaffInvite(Base):
    """One-time email invite for a staff member to set their password."""
    __tablename__ = "staff_invites"

    id         = Column(Integer, primary_key=True, index=True)
    clinic_id  = Column(Integer, ForeignKey("clinics.id"), nullable=False, index=True)
    email      = Column(String(150), nullable=False)
    token      = Column(String(100), unique=True, index=True, nullable=False)
    role       = Column(String(20), default="receptionist")
    expires_at = Column(DateTime, nullable=False)
    used_at    = Column(DateTime, nullable=True)

    clinic = relationship("Clinic", back_populates="invites")


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
    created_at = Column(DateTime, default=datetime.utcnow)

    appointments       = relationship("Appointment", back_populates="doctor", cascade="all, delete-orphan")
    patients           = relationship("Patient", back_populates="doctor", cascade="all, delete-orphan")
    schedules          = relationship("DoctorSchedule", back_populates="doctor", cascade="all, delete-orphan")
    blocked_dates      = relationship("BlockedDate", back_populates="doctor", cascade="all, delete-orphan")
    subscriptions      = relationship("Subscription", back_populates="doctor", cascade="all, delete-orphan")
    clinic_memberships = relationship("ClinicDoctor", back_populates="doctor")


# --------------------------------------------------------------------------- #
#  Patient                                                                      #
# --------------------------------------------------------------------------- #

class Patient(Base):
    __tablename__ = "patients"

    id = Column(Integer, primary_key=True, index=True)
    doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=False, index=True)
    clinic_id = Column(Integer, ForeignKey("clinics.id"), nullable=True, index=True)   # Phase 2
    name = Column(String(100), nullable=False)
    phone = Column(String(15), nullable=False)
    language_pref = Column(String(20), default="english")
    notes = Column(Text, nullable=True)
    visit_count = Column(Integer, default=0)
    first_visit = Column(Date, nullable=True)
    last_visit = Column(Date, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    doctor       = relationship("Doctor", back_populates="patients")
    appointments = relationship("Appointment", back_populates="patient", cascade="all, delete-orphan")
    note_entries = relationship(
        "PatientNote", back_populates="patient",
        cascade="all, delete-orphan",
        order_by="PatientNote.created_at.desc()",
    )


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
#  Appointment                                                                  #
# --------------------------------------------------------------------------- #

class Appointment(Base):
    __tablename__ = "appointments"

    id = Column(Integer, primary_key=True, index=True)
    doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=False, index=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False, index=True)
    clinic_id = Column(Integer, ForeignKey("clinics.id"), nullable=True, index=True)   # Phase 2
    staff_id  = Column(Integer, ForeignKey("staff.id"), nullable=True)                  # Phase 2: who booked
    appointment_date = Column(Date, nullable=False)
    appointment_time = Column(Time, nullable=False)
    duration_mins = Column(Integer, default=15)
    appointment_type = Column(SAEnum(AppointmentType), default=AppointmentType.follow_up)
    status = Column(SAEnum(AppointmentStatus), default=AppointmentStatus.scheduled, index=True)
    patient_notes = Column(Text, nullable=True)
    doctor_notes = Column(Text, nullable=True)
    reminder_24h_sent = Column(Boolean, default=False)
    reminder_2h_sent = Column(Boolean, default=False)
    booked_by = Column(SAEnum(BookedBy), default=BookedBy.doctor)
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
    appointment_id = Column(Integer, ForeignKey("appointments.id"), nullable=False, index=True)
    type = Column(SAEnum(NotificationType), nullable=False)
    channel = Column(SAEnum(NotificationChannel), nullable=False)
    message_body = Column(Text, nullable=True)
    status = Column(String(20), default="pending")  # pending, sent, failed
    sent_at = Column(DateTime, nullable=True)

    appointment = relationship("Appointment", back_populates="notifications")
