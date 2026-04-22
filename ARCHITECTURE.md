# ClinicOS — Architecture & Use Case Documentation

## Overview

ClinicOS is built around one core model: **one doctor account = one clinic**. Every piece of data in the system is owned by a `doctor_id`. This works perfectly for the primary customer (a solo GP in a Tier 2 city) but the architecture has deliberate extension points for more complex setups.

This document covers:
1. How the current system is structured
2. Four real-world practice configurations and how they map to ClinicOS
3. What works today, what needs to be built, and how to build it

---

## Current Data Model (What Exists Today)

```
doctors
  └─── patients            (doctor_id FK)
  └─── appointments        (doctor_id FK, patient_id FK)
  └─── doctor_schedules    (doctor_id FK)
  └─── blocked_dates       (doctor_id FK)
  └─── subscriptions       (doctor_id FK)
  └─── notifications_log   (appointment_id FK)
```

Every table ties back to a single `doctor_id`. There is no "clinic", no "staff", no "location" entity yet. This is intentional — it keeps the MVP lean and the data model simple.

### Key identifiers

| Field | Table | Purpose |
|---|---|---|
| `doctor.slug` | doctors | Public booking URL (`/book/{slug}`) — unique per doctor |
| `appointment.booked_by` | appointments | Enum: `doctor` or `patient` — who initiated the booking |
| `appointment.status` | appointments | `scheduled → completed / cancelled / no_show` |
| `doctor_schedule.day_of_week` | doctor_schedules | 0=Monday … 6=Sunday, one row per active day |

---

## The Four Practice Configurations

### Configuration 1 — Solo Doctor, Single Clinic

**Example:** Dr. Rajesh Mehta, GP, Mehta Clinic, Nashik.

This is the primary target customer. One doctor, one clinic, one address, one booking URL.

```
Dr. Mehta (doctor account)
  → Booking URL: /book/dr-rajesh-mehta-nashik
  → Schedule: Mon–Sat, 9am–6pm, 15-min slots
  → Patients book online or doctor books from dashboard
  → WhatsApp confirmation sent to patient
  → Doctor sees everything on dashboard
```

**How ClinicOS handles this today: Fully supported. Zero gaps.**

The entire current system is designed for this scenario. Doctor registers, gets a booking link, shares it on WhatsApp with patients, done.

---

### Configuration 2 — Doctor with Receptionist

**Example:** Dr. Mehta's clinic is busy. He has a receptionist, Priya, who answers the phone, books walk-in appointments, and manages the diary. Dr. Mehta only wants to see patients and write notes — not manage bookings himself.

**What Priya needs to do:**
- Book new appointments (phone walk-ins)
- Cancel or reschedule appointments on patient request
- Check today's schedule to tell patients wait times
- Mark patients as arrived

**What Priya should NOT be able to do:**
- See financial reports or billing
- Change clinic settings or working hours
- Access another doctor's data

**How ClinicOS handles this today:**

There is no staff/receptionist concept. The only workaround is sharing Dr. Mehta's login credentials with Priya — which is a security risk. Priya would have full access to everything including billing and settings.

**What needs to be built — Staff Module:**

Add a `staff` table:

```python
class Staff(Base):
    __tablename__ = "staff"

    id            = Column(Integer, primary_key=True)
    doctor_id     = Column(Integer, ForeignKey("doctors.id"))   # which doctor they work for
    name          = Column(String(100), nullable=False)
    email         = Column(String(150), unique=True, nullable=False)
    phone         = Column(String(15))
    password_hash = Column(String(255), nullable=False)
    role          = Column(String(20), default="receptionist")  # receptionist | manager
    is_active     = Column(Boolean, default=True)
    created_at    = Column(DateTime, default=datetime.utcnow)
```

Extend the `BookedBy` enum:
```python
class BookedBy(str, enum.Enum):
    doctor      = "doctor"
    patient     = "patient"
    staff       = "staff"       # ← new
```

Add `staff_id` to Appointment:
```python
staff_id = Column(Integer, ForeignKey("staff.id"), nullable=True)
```

**Auth changes:**
- Staff log in at the same `/login` page with their own email/password
- JWT payload includes `{"staff_id": 5, "doctor_id": 3, "role": "receptionist"}`
- `get_current_user()` dependency returns either a Doctor or Staff object
- A new `get_staff_or_doctor()` dependency: accepts both, returns the `doctor_id` for data queries
- Routes accessible to staff: appointments (all), patients (list + detail + notes)
- Routes blocked for staff: reports, billing, settings (working hours, profile, blocked dates, subscription)

**Permission matrix:**

| Feature | Doctor | Receptionist |
|---|---|---|
| Book appointment | ✅ | ✅ |
| View / edit appointment | ✅ | ✅ |
| Mark complete / no-show | ✅ | ✅ |
| Patient records & notes | ✅ | ✅ |
| Calendar view | ✅ | ✅ |
| Reports & analytics | ✅ | ❌ |
| Settings (schedule, profile) | ✅ | ❌ |
| Billing & subscription | ✅ | ❌ |
| Add / remove staff | ✅ | ❌ |

**Doctor side:** A new Settings section — "Staff" — where doctor can invite a receptionist by email, see active staff, revoke access.

**Staff login flow:**
```
Staff visits /login
  → enters their email + password
  → server finds staff row, verifies password
  → JWT issued with staff_id + doctor_id
  → redirected to /appointments (not /dashboard — staff don't see the doctor's stats)
```

---

### Configuration 3 — Single Doctor, Multiple Locations

**Example:** Dr. Sharma is a visiting cardiologist. She consults at:
- Sharma Heart Clinic (her own): Mon, Wed, Fri — 10am–4pm
- Apollo Nashik (visiting): Tue, Thu — 9am–1pm

Patients booking at Apollo should see Apollo's address and Apollo's available slots. Patients booking at Sharma Clinic see different availability.

**How ClinicOS handles this today:**

The schedule is per `day_of_week` with no location concept. If Dr. Sharma sets Tuesday schedule to 9am–1pm, the public booking form shows her clinic address (Sharma Heart Clinic) even though Tuesday she's at Apollo. There's no way to show different addresses or slot windows for different locations from one account.

**Workaround available today (no code changes needed):**
Dr. Sharma creates **two separate doctor accounts**:
- `dr-sharma-heart-clinic` — schedules Mon/Wed/Fri, shows Sharma Clinic address
- `dr-sharma-apollo-nashik` — schedules Tue/Thu, shows Apollo address

She shares two different booking links. Two completely separate patient databases though, which is the downside.

**What needs to be built — Locations Module:**

Add a `doctor_locations` table:

```python
class DoctorLocation(Base):
    __tablename__ = "doctor_locations"

    id         = Column(Integer, primary_key=True)
    doctor_id  = Column(Integer, ForeignKey("doctors.id"))
    name       = Column(String(150), nullable=False)   # "Apollo Nashik"
    address    = Column(Text)
    city       = Column(String(100))
    slug       = Column(String(100), unique=True)       # /book/dr-sharma-apollo
    is_active  = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
```

Extend `DoctorSchedule`:
```python
location_id = Column(Integer, ForeignKey("doctor_locations.id"), nullable=True)
# NULL = primary clinic schedule
# Set = schedule for that specific location
```

Extend `Appointment`:
```python
location_id = Column(Integer, ForeignKey("doctor_locations.id"), nullable=True)
# Records which location this appointment is at
```

**Public booking URLs:**
```
/book/{doctor-slug}                      # default location
/book/{doctor-slug}/{location-slug}      # specific location
```

Each location gets its own slug so Dr. Sharma can share:
- `clinicos.app/book/dr-sharma-apollo-nashik` → shows Apollo address, Tue/Thu slots only
- `clinicos.app/book/dr-sharma-heart-clinic` → shows her clinic address, Mon/Wed/Fri slots

Both booking flows land in the same doctor account — same patient database, same dashboard, unified calendar with location labels per appointment.

**Appointment detail with locations:**
```
Appointment #42
  Patient: Rahul Verma
  Date: Tuesday, 29 Apr 2026
  Time: 10:30 AM
  Location: Apollo Nashik         ← shown clearly
  Status: Scheduled
```

**Settings addition:** A "Locations" tab where doctor can add/edit their consultation sites, each with its own schedule override.

---

### Configuration 4 — Multiple Doctors, One Clinic

**Example:** Fortis Nashik has three doctors:
- Dr. Mehta — General Physician
- Dr. Sharma — Cardiologist
- Dr. Patel — Paediatrician

The clinic has a shared receptionist and a clinic manager who wants to see all doctors' calendars and overall appointment volume.

**How ClinicOS handles this today:**

Each doctor creates their own separate ClinicOS account. This actually works surprisingly well:

```
Dr. Mehta:   /book/dr-mehta-nashik
Dr. Sharma:  /book/dr-sharma-nashik
Dr. Patel:   /book/dr-patel-nashik
```

Each doctor's data is completely siloed. Each pays their own ₹299–499/month separately. The receptionist cannot see all three calendars in one place — she'd need to log into three different accounts.

**What works today without any changes:**
- Patients can book with any specific doctor via their individual URL
- Each doctor manages their own schedule independently
- Each doctor sees their own reports
- Billing is separate per doctor

**What doesn't work:**
- No unified `/book/fortis-nashik` page where a patient can browse all doctors
- No receptionist account that can see and manage all three doctors
- No clinic-level admin dashboard
- Each doctor pays separately — no "clinic plan"

**What needs to be built — Clinic Entity:**

Add a `clinics` table:

```python
class Clinic(Base):
    __tablename__ = "clinics"

    id              = Column(Integer, primary_key=True)
    name            = Column(String(150), nullable=False)      # "Fortis Nashik"
    address         = Column(Text)
    city            = Column(String(100))
    slug            = Column(String(100), unique=True)          # /book/clinic/fortis-nashik
    owner_doctor_id = Column(Integer, ForeignKey("doctors.id")) # who manages billing
    is_active       = Column(Boolean, default=True)
    created_at      = Column(DateTime, default=datetime.utcnow)
```

Add a `doctor_clinic` junction table:

```python
class DoctorClinic(Base):
    __tablename__ = "doctor_clinic"

    id         = Column(Integer, primary_key=True)
    doctor_id  = Column(Integer, ForeignKey("doctors.id"))
    clinic_id  = Column(Integer, ForeignKey("clinics.id"))
    role       = Column(String(20), default="associate")  # owner | associate | visiting
    joined_at  = Column(DateTime, default=datetime.utcnow)
```

**New public booking URL:**
```
/book/clinic/fortis-nashik
  → Lists all active doctors with their specializations
  → Patient picks doctor → sees that doctor's available slots
  → Appointment still tied to doctor_id (data stays siloed at doctor level)
```

**Clinic admin view** (`/clinic/dashboard`):
- Today's appointments across all doctors (read-only aggregated view)
- Which doctor has the most slots available right now
- Overall clinic stats: total patients, completion rate across all doctors

**Clinic receptionist:**
```python
class Staff(Base):
    doctor_id = Column(Integer, ForeignKey("doctors.id"), nullable=True)  # if working for one doctor
    clinic_id = Column(Integer, ForeignKey("clinics.id"), nullable=True)  # if working for whole clinic
    # one of the two must be set
```

A clinic-level receptionist can book appointments for ANY doctor in the clinic, not just one.

**Clinic billing:**
Owner doctor pays one plan that covers all doctors in the clinic:
- Clinic Basic: ₹999/month (up to 3 doctors)
- Clinic Pro: ₹1,999/month (unlimited doctors)
- Individual doctors in the clinic don't pay separately

---

## Summary: What Works Today vs What Needs Building

| Scenario | Works Today | Gap | Effort |
|---|---|---|---|
| Solo doctor, single clinic | ✅ Fully works | — | — |
| Doctor + receptionist | ⚠️ Share login (risky) | Staff accounts + permission system | Medium |
| Doctor, multiple locations | ⚠️ Two accounts workaround | Locations table + location slugs | Medium |
| Multiple doctors, one clinic | ⚠️ Separate accounts | Clinic entity + unified booking + clinic admin | Large |
| Clinic receptionist (all doctors) | ❌ Not possible | Staff with clinic_id + cross-doctor access | Large |

---

## Recommended Build Order

If you want to extend ClinicOS to cover all these cases, build in this order:

### Step 1 — Staff / Receptionist (2–3 days)
Highest demand. Almost every doctor with more than 20 patients/day has someone helping them. Builds on existing auth pattern.

Files to create:
- `database/models.py` → add `Staff` model, extend `BookedBy` enum
- `services/auth_service.py` → add `get_current_user()` that handles both doctor and staff JWT
- `routers/staff.py` → staff login, invite staff (doctor sends email invite), list staff
- `templates/staff_login.html` → same design as login.html
- `templates/settings.html` → add "Staff" section: invite form + active staff list

### Step 2 — Multiple Locations (1–2 days)
Second most common for specialists and visiting consultants. Relatively contained change.

Files to modify:
- `database/models.py` → add `DoctorLocation` model
- `database/models.py` → add `location_id` FK to `DoctorSchedule` and `Appointment`
- `routers/public.py` → add `GET /book/{doctor-slug}/{location-slug}` route
- `routers/doctors.py` → add location management to settings
- `templates/settings.html` → add "Locations" section
- `templates/appointment_detail.html` → show location name

### Step 3 — Multi-Doctor Clinic (3–5 days)
Most complex. Only needed when targeting hospital chains or polyclinics as customers.

Files to create:
- `database/models.py` → add `Clinic`, `DoctorClinic` models
- `routers/clinic.py` → create clinic, invite doctors, unified booking page
- `templates/public_clinic.html` → doctor picker page
- `templates/clinic_dashboard.html` → aggregated clinic admin view
- Extend `Staff` model with `clinic_id`

---

## How the Current Booking Flow Works (End to End)

Understanding this is essential before extending the system.

```
PATIENT BOOKS ONLINE
─────────────────────────────────────────────────────────────────────
1. Patient gets link: clinicos.app/book/dr-rajesh-mehta-nashik

2. GET /book/{slug}
   → Looks up Doctor by slug (must be is_active=True)
   → Calls get_available_slots(doctor_id, today) which:
       a. Fetches DoctorSchedule for today's weekday
       b. Generates all time slots between start_time and end_time
       c. Removes slots already booked (Appointment query, non-cancelled)
       d. Removes the date if it's in BlockedDate
       e. Removes slots where appointment count >= max_patients
   → Renders public_booking.html with available slots

3. Patient fills name, phone, date, time, visit type

4. POST /book/{slug}
   → Rate limit check: max 5 bookings from this phone in 24h
   → is_slot_available() — re-validates (guards against race conditions)
   → get_or_create_patient() — finds existing patient by (doctor_id, phone)
     or creates a new Patient row
   → Creates Appointment row with booked_by=BookedBy.patient
   → Updates patient.visit_count, last_visit, first_visit
   → Fires notify_appointment_confirmed() — WhatsApp message sent
   → Redirects to /book/{slug}/confirm/{appt_id}

5. Confirmation page shows:
   → Appointment summary
   → Add to Google Calendar link
   → Clinic address
─────────────────────────────────────────────────────────────────────

DOCTOR BOOKS FROM DASHBOARD
─────────────────────────────────────────────────────────────────────
1. Doctor opens /appointments/new (or clicks today's date in calendar)

2. GET /appointments/new
   → Same get_available_slots() call
   → Renders appointment_new.html with date picker + slot dropdown

3. Doctor enters patient name + phone
   → As doctor types phone, patient can be looked up
   → If new patient: Patient row created on submit

4. POST /appointments
   → is_slot_available() validates slot
   → get_or_create_patient() same as above
   → Creates Appointment with booked_by=BookedBy.doctor
   → Fires notify_appointment_confirmed()
   → Redirects to appointment detail page
─────────────────────────────────────────────────────────────────────

REMINDER SCHEDULER (every 15 minutes)
─────────────────────────────────────────────────────────────────────
APScheduler background job:
1. Query all Appointments where status=scheduled AND reminder_24h_sent=False
   → For each: if appointment_datetime is 23–25h from now → send WhatsApp
   → Set reminder_24h_sent=True

2. Query all Appointments where status=scheduled AND reminder_2h_sent=False
   → For each: if appointment_datetime is 90–150 min from now → send WhatsApp
   → Set reminder_2h_sent=True
─────────────────────────────────────────────────────────────────────
```

---

## How Plan Gating Works

Every doctor-facing route (except `/billing`, `/login`, `/register`, `/logout`) uses `Depends(get_paying_doctor)`.

```
Request hits /appointments
  → get_paying_doctor() runs
  → Calls get_current_doctor() → validates JWT cookie → returns Doctor object
  → Checks: is trial_ends_at > now?  OR  is plan_expires_at > now?
  → If YES → returns doctor, request proceeds normally
  → If NO  → raises PlanExpired exception
             → main.py exception handler catches it
             → RedirectResponse("/billing", 303)
```

The `/billing` route uses `Depends(get_current_doctor)` only (no plan check) so an expired doctor can always reach the billing page to subscribe.

---

## Security Boundaries

The most important rule in ClinicOS: **doctors must never see other doctors' data.**

Every single database query that touches patient, appointment, or schedule data filters by `doctor_id`:

```python
# This pattern appears on every query — NEVER omit the doctor_id filter
db.query(Appointment).filter(
    Appointment.doctor_id == doctor.id,   # ← this line is non-negotiable
    Appointment.id == appt_id,
).first()
```

If you add a staff receptionist, the same rule applies:
```python
# Staff can only query the doctor they work for
db.query(Appointment).filter(
    Appointment.doctor_id == staff.doctor_id,   # staff carries doctor_id in JWT
    ...
)
```

If you add a clinic entity, the clinic admin can only see doctors in their clinic:
```python
# Clinic admin queries
doctor_ids = [dc.doctor_id for dc in clinic.doctors]
db.query(Appointment).filter(
    Appointment.doctor_id.in_(doctor_ids),
    ...
)
```

---

## Patient Identity

Currently a patient is identified by `(doctor_id, phone)` — the same phone number under two different doctors is two separate Patient records. This is intentional:

- Dr. Mehta's Priya (patient) has notes about chronic back pain
- Dr. Sharma's Priya (same person) has cardiology notes
- These should never merge — different doctors, different medical context, different notes

If a future "clinic" feature is added, a patient could be represented at the clinic level too, but the individual doctor-patient records should stay separate.

---

*Last updated: 2026-04-22*
