# ClinicOS — Claude Code Project Memory

## What This Project Is
ClinicOS is a SaaS appointment management system for independent doctors in Indian Tier 2/3 cities.
Priced at ₹299–499/month. WhatsApp-first, regional language support, mobile-friendly.

Target customer: Dr. Mehta in Nashik — a GP with no digital system, using a paper register.

---

## Three User Types
- **Doctor** — pays ₹299–499/month, manages appointments, sees calendar, gets reports
- **Patient** — books via public link, receives WhatsApp/SMS reminders, no login needed
- **Admin** — platform owner (me), manages all doctors, billing, platform stats

---

## Tech Stack (Do Not Change Without Asking)
| Layer | Tool | Notes |
|---|---|---|
| Backend | FastAPI (Python) | Main framework |
| Frontend | HTML + CSS + Vanilla JS | No React, no Vue |
| Templates | Jinja2 | Server-side rendering |
| Database (dev) | SQLite | File: `clinic.db` |
| Database (prod) | PostgreSQL | Railway.app managed |
| ORM | SQLAlchemy | Python classes, not raw SQL |
| Auth | JWT + Passlib (bcrypt) | Token-based login |
| WhatsApp/SMS | Twilio | Primary notification service |
| Payments | Razorpay | Indian UPI/card payments |
| Scheduler | APScheduler | Background reminder jobs |
| Deployment | Railway.app | Auto-deploy from GitHub |

---

## Folder Structure
```
clinicos/
├── main.py                  # Entry point — registers all routers, starts app
├── config.py                # Settings loaded from .env
├── requirements.txt         # All pip packages
├── .env                     # Secret keys — NEVER commit this
├── CLAUDE.md                # This file
│
├── database/
│   ├── __init__.py
│   ├── connection.py        # Creates DB engine + session
│   └── models.py            # All SQLAlchemy table definitions
│
├── routers/
│   ├── __init__.py
│   ├── auth.py              # /register, /login, /logout
│   ├── appointments.py      # /appointments — CRUD
│   ├── doctors.py           # /doctors — profile, settings, schedule
│   ├── patients.py          # /patients — list, profile
│   ├── public.py            # /book/{slug} — no auth needed
│   └── admin.py             # /admin — platform owner only
│
├── services/
│   ├── __init__.py
│   ├── auth_service.py      # Password hash, JWT create/verify
│   ├── appointment_service.py # Slot availability, booking rules
│   ├── notification_service.py # Twilio WhatsApp + SMS sending
│   ├── payment_service.py   # Razorpay order create + verify
│   └── scheduler_service.py # APScheduler — reminder jobs
│
├── templates/
│   ├── base.html            # Master layout: navbar, footer
│   ├── login.html
│   ├── register.html
│   ├── dashboard.html
│   ├── calendar.html
│   ├── add_appointment.html
│   ├── patients.html
│   ├── patient_detail.html
│   ├── settings.html
│   ├── reports.html
│   ├── billing.html
│   ├── public_booking.html
│   └── admin/
│       ├── admin_dashboard.html
│       └── doctors_list.html
│
└── static/
    ├── css/
    │   ├── main.css
    │   └── calendar.css
    ├── js/
    │   ├── calendar.js
    │   ├── booking.js
    │   └── dashboard.js
    └── img/
        └── logo.png
```

---

## Database Tables (Summary)
- **doctors** — id, name, email, phone, password_hash, specialization, clinic_name, clinic_address, city, languages, is_active, plan_type, trial_ends_at, plan_expires_at, created_at
- **patients** — id, doctor_id, name, phone, language_pref, notes, visit_count, first_visit, last_visit, created_at
- **appointments** — id, doctor_id, patient_id, appointment_date, appointment_time, duration_mins, appointment_type, status, patient_notes, doctor_notes, reminder_24h_sent, reminder_2h_sent, created_at, booked_by
- **doctor_schedules** — id, doctor_id, day_of_week, start_time, end_time, slot_duration, max_patients, is_active
- **blocked_dates** — id, doctor_id, blocked_date, reason
- **subscriptions** — id, doctor_id, plan_name, amount, payment_id, start_date, end_date, status
- **notifications_log** — id, appointment_id, type, channel, message_body, status, sent_at

---

## Coding Rules (Always Follow)
1. **Never store plain passwords** — always use `passlib` bcrypt hashing
2. **Never hardcode secrets** — all keys come from `.env` via `config.py`
3. **Always filter by doctor_id** — doctors must never see other doctors' data
4. **Validate inputs server-side** — never trust frontend data
5. **Rate limit public booking** — max 5 bookings per phone per 24h
6. **Keep routes thin** — business logic belongs in services/, not routers/
7. **One feature at a time** — build and test before moving to next feature

---

## Subscription Plans
- **Free Trial** — 14 days, full access, no card needed
- **Basic** — ₹299/month, up to 30 appointments/day, reminders, public booking
- **Pro** — ₹499/month, unlimited appointments, two-way WhatsApp, analytics, export

---

## Build Order (Current Progress Tracker)
Update the status column as features are completed.

| # | Feature | Status |
|---|---|---|
| 1 | Project setup + virtual environment | ⬜ Not started |
| 2 | database/models.py — all 7 tables | ⬜ Not started |
| 3 | database/connection.py | ⬜ Not started |
| 4 | config.py + .env setup | ⬜ Not started |
| 5 | main.py — base FastAPI app | ⬜ Not started |
| 6 | auth — register + login + JWT | ⬜ Not started |
| 7 | Dashboard page (basic, shows no data yet) | ⬜ Not started |
| 8 | Schedule settings (working hours, slot duration) | ⬜ Not started |
| 9 | Appointment creation form (backend + frontend) | ⬜ Not started |
| 10 | Calendar view | ⬜ Not started |
| 11 | Public booking page | ⬜ Not started |
| 12 | Slot availability logic (no double-booking) | ⬜ Not started |
| 13 | Patient profile pages | ⬜ Not started |
| 14 | WhatsApp/SMS notifications (Twilio) | ⬜ Not started |
| 15 | Background reminder scheduler (APScheduler) | ⬜ Not started |
| 16 | Two-way WhatsApp reply handling | ⬜ Not started |
| 17 | Razorpay payment integration | ⬜ Not started |
| 18 | Subscription plan gating | ⬜ Not started |
| 19 | Reports + analytics page | ⬜ Not started |
| 20 | Admin panel | ⬜ Not started |
| 21 | Deploy on Railway.app | ⬜ Not started |

---

## Key Business Rules to Always Enforce
- A slot is unavailable if: another appointment exists at same date+time for same doctor, OR the time is outside doctor's schedule hours, OR the date is in blocked_dates, OR max_patients for that shift is reached
- Reminders fire at: T-24h and T-2h before appointment_date + appointment_time
- No-show auto-trigger: if status not updated to 'completed' within 30 min after appointment end time, system flags it for doctor review
- Free trial: 14 days from created_at on doctors table
- Plan expiry check: run on every protected route — if plan_expires_at < today AND trial_ends_at < today, redirect to billing page

---

## Environment Variables Needed (.env file)
```
DATABASE_URL=sqlite:///./clinic.db
SECRET_KEY=your-jwt-secret-key-here
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=1440

TWILIO_ACCOUNT_SID=your-twilio-sid
TWILIO_AUTH_TOKEN=your-twilio-token
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886

RAZORPAY_KEY_ID=your-razorpay-key
RAZORPAY_KEY_SECRET=your-razorpay-secret
```

---

## What "Done" Means for Each Feature
A feature is only done when:
- [ ] Backend route works (tested in browser or Postman)
- [ ] Frontend page displays correctly on mobile screen size
- [ ] Data is correctly saved/retrieved from database (verified in DB Browser)
- [ ] No hardcoded values — everything comes from DB or .env
- [ ] Tested with wrong inputs (empty form, wrong password, double booking attempt)

---

## Session Startup Checklist
When starting a new Claude Code session, say:
> "Read CLAUDE.md. We are continuing ClinicOS. Last completed feature was [X]. Today we are building [Y]. Here is the current state of the relevant files: [paste file contents if needed]."

---

*Last updated: [update this date each session]*
*Current phase: Setup*
