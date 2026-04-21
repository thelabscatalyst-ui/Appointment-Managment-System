from datetime import datetime, date, time
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Date, Time,
    ForeignKey, Text, Enum as SAEnum
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
    basic = "basic"
    pro = "pro"


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
    doctor = "doctor"
    patient = "patient"


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
    is_active = Column(Boolean, default=True)
    plan_type = Column(SAEnum(PlanType), default=PlanType.trial)
    trial_ends_at = Column(DateTime, nullable=True)
    plan_expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    appointments = relationship("Appointment", back_populates="doctor", cascade="all, delete-orphan")
    patients = relationship("Patient", back_populates="doctor", cascade="all, delete-orphan")
    schedules = relationship("DoctorSchedule", back_populates="doctor", cascade="all, delete-orphan")
    blocked_dates = relationship("BlockedDate", back_populates="doctor", cascade="all, delete-orphan")
    subscriptions = relationship("Subscription", back_populates="doctor", cascade="all, delete-orphan")


# --------------------------------------------------------------------------- #
#  Patient                                                                      #
# --------------------------------------------------------------------------- #

class Patient(Base):
    __tablename__ = "patients"

    id = Column(Integer, primary_key=True, index=True)
    doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    phone = Column(String(15), nullable=False)
    language_pref = Column(String(20), default="english")
    notes = Column(Text, nullable=True)
    visit_count = Column(Integer, default=0)
    first_visit = Column(Date, nullable=True)
    last_visit = Column(Date, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    doctor = relationship("Doctor", back_populates="patients")
    appointments = relationship("Appointment", back_populates="patient", cascade="all, delete-orphan")


# --------------------------------------------------------------------------- #
#  Appointment                                                                  #
# --------------------------------------------------------------------------- #

class Appointment(Base):
    __tablename__ = "appointments"

    id = Column(Integer, primary_key=True, index=True)
    doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=False, index=True)
    patient_id = Column(Integer, ForeignKey("patients.id"), nullable=False, index=True)
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
