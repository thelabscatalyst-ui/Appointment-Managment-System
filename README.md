# Nivora

Clinic management platform built for independent doctors and small clinics in India. Handles appointments, live patient queue, billing with PDF receipts, patient records with document vault, WhatsApp notifications, income tracking, and subscription management — all from a single web app. No app download needed for patients.

**Live:** Deployed on Railway with PostgreSQL.

---

## Table of Contents

- [Features](#features)
- [Tech Stack](#tech-stack)
- [Architecture](#architecture)
- [Project Structure](#project-structure)
- [Local Setup](#local-setup)
- [Environment Variables](#environment-variables)
- [Database](#database)
- [Queue System](#queue-system)
- [Billing Flow](#billing-flow)
- [Notification System](#notification-system)
- [Subscription & Payments](#subscription--payments)
- [Multi-Doctor Clinic](#multi-doctor-clinic)
- [Security](#security)
- [Public Booking](#public-booking)
- [Design System](#design-system)
- [Deployment](#deployment)
- [API Reference](#api-reference)

---

## Features

### Appointments & Live Queue
- Live token queue — check in walk-ins, call next, serve, skip, promote emergencies
- Queue state machine: `Waiting` → `Serving` → `Billing Pending` → `Done`
- Walk-in quick booking with automatic check-in
- Returning patient detection — entering a known phone shows a **Returning** badge with visit count
- Slot-based scheduling with conflict prevention (no double booking)
- Create, edit, reschedule, and cancel appointments
- Appointment detail card overlay with full patient history
- Booking channel badges: Walk-in, Doctor, Patient, Reception
- Monthly calendar view with daily appointment counts
- Public queue display at `/queue/{slug}` — TV screen showing current token, auto-refreshes
- Today's Flow bar — live counts for scheduled, checked-in, completed, and on-time rate

### Billing & Invoicing
- Collect Payment modal — line items with quantity, discount, GST, payment mode (Cash / UPI / Card / Insurance / Free)
- Price catalog — preset consultation fees as quick-fill buttons in the billing modal
- Edit bills after saving — update items, discount, payment mode
- PDF bill generation on-demand (no storage, generated fresh on download)
- Bill detail page with full breakdown and PDF download
- Mark pending bills as paid from the collections page

### Income & Expenses
- Income dashboard — daily/monthly revenue with visual charts
- Transaction history with date-range filtering
- Expense tracker — log clinic expenses by category (Rent, Salaries, Medicines, Equipment, Utilities, Marketing, Misc)
- Recurring expenses — auto-fire on a set day each month
- Net profit calculations (income minus expenses)
- All financial pages are PIN-protected

### Patients
- Patient list with search and alphabetical navigation
- Patient profile — visit history, visit count, first/last visit dates
- Medical details: age, gender, blood group, allergies, language preference
- Doctor notes with file attachments per visit
- Document vault — upload, categorize (Lab Report, Prescription, X-Ray, Insurance, etc.), and search files
- Pin frequently visited patients for quick access
- Delete patient with full cascade (PIN-protected)

### Settings
- Configurable working hours per day of week
- Slot duration and max patients per schedule
- Walk-in buffer slots reserved for emergencies
- Block specific dates (holidays) and time windows
- Profile management — name, email, phone, specialization, clinic details
- Subscription management with smart routing (trial → pricing, active → billing)
- 6-digit PIN setup and management
- Dark / light theme toggle

### Reports & Analytics
- Appointment completion and no-show rates
- Top patients by visit count
- Monthly appointment trends (line chart)
- Revenue metrics and comparisons
- PIN-protected

### Notifications (WhatsApp)
- Booking confirmation sent immediately after appointment creation
- Follow-up appointment confirmation with "bring previous reports" note
- 24-hour reminder (background scheduler)
- 2-hour reminder (background scheduler)
- Walk-in queue position + estimated wait time notification
- Itemized bill receipt with payment details
- All notifications logged in database with sent/failed status

### Public Booking (No Login Required)
- Patients book via a shareable URL — `/book/{doctor-slug}`
- Clinic-wide booking page — `/book/clinic/{clinic-slug}`
- Slot availability shown in real-time
- WhatsApp confirmation sent on booking
- Google Calendar link on confirmation page
- Rate-limited: max 5 bookings per phone number per 24 hours

### Multi-Doctor Clinic
- Multiple doctors under one clinic entity
- Clinic admin dashboard — aggregated stats across all doctors
- Doctor invite system via email with one-time accept links
- Unified public booking page with doctor selection
- Clinic plan billing — owner's subscription covers all associate doctors
- PIN-protected admin authentication

### Platform Admin
- Admin panel at `/admin` — all registered doctors, plan statuses
- Active trials, paid plans, expired accounts overview
- Platform-level revenue metrics

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | FastAPI 0.136 (Python) |
| Templating | Jinja2 — server-side rendered HTML |
| Frontend | Vanilla HTML + CSS + JavaScript (no framework) |
| Database | SQLite (dev) / PostgreSQL (production) |
| ORM | SQLAlchemy 2.0 |
| Auth | JWT stored in HTTP-only cookies, bcrypt password hashing |
| PDF Generation | fpdf2 |
| Notifications | Twilio WhatsApp + SMS |
| Payments | Razorpay (UPI, Cards, Net Banking, Wallets) |
| Background Jobs | APScheduler (reminder + auto no-show) |
| Deployment | Railway.app |

---

## Architecture

```
Client (Browser)
     │
     ▼
┌─────────────────────────────────────────┐
│  FastAPI Application (main.py)          │
│                                         │
│  Middleware:                             │
│  ├── Security headers                   │
│  ├── Login rate limiter (10/15min/IP)   │
│  └── Clinic owner state injection       │
│                                         │
│  Routers:                               │
│  ├── auth        → /login, /register    │
│  ├── doctors     → /dashboard, /settings│
│  ├── appointments→ /appointments/*      │
│  ├── visits      → /visits/*, /queue/*  │
│  ├── billing_ops → /bills/*, /catalog   │
│  ├── income      → /income, /expenses   │
│  ├── patients    → /patients/*          │
│  ├── clinic      → /clinic/admin/*      │
│  ├── public      → /book/*             │
│  └── admin       → /admin/*            │
│                                         │
│  Services:                              │
│  ├── auth_service         (JWT + PIN)   │
│  ├── appointment_service  (slots)       │
│  ├── visit_service        (queue logic) │
│  ├── notification_service (WhatsApp)    │
│  ├── payment_service      (Razorpay)    │
│  ├── bill_pdf_service     (PDF gen)     │
│  ├── invite_service       (SMTP email)  │
│  └── scheduler_service    (APScheduler) │
└─────────────────────────────────────────┘
     │                    │
     ▼                    ▼
PostgreSQL          Razorpay API
(Railway)           (Payments)
                         │
                    Twilio API
                    (WhatsApp/SMS)
```

---

## Project Structure

```
Nivora/
├── main.py                        # App entry, middleware, exception handlers, router mounts
├── config.py                      # Pydantic Settings — loads .env
├── requirements.txt               # Python dependencies
│
├── database/
│   ├── connection.py              # SQLAlchemy engine, session, create_tables(), migrations
│   └── models.py                  # All ORM models (18 models) and enums (12 enums)
│
├── routers/
│   ├── auth.py                    # Registration, login, logout
│   ├── doctors.py                 # Dashboard, settings, calendar, reports, pricing, billing, PIN
│   ├── appointments.py            # Appointment CRUD, walk-in, queue display, slot API
│   ├── visits.py                  # Queue state machine, check-in, call, done, skip, emergency
│   ├── billing_ops.py             # Bill creation/edit, PDF download, price catalog CRUD
│   ├── income.py                  # Income dashboard, transactions, expenses, recurring expenses
│   ├── patients.py                # Patient CRUD, notes, file attachments, document vault
│   ├── clinic.py                  # Clinic admin, doctor invites, multi-doctor management
│   ├── public.py                  # Public booking pages (no auth), rate-limited
│   └── admin.py                   # Platform admin panel
│
├── services/
│   ├── auth_service.py            # JWT creation/decode, password hashing, PIN sessions
│   │                                Dependencies: get_current_doctor, get_paying_doctor,
│   │                                require_pin, PlanExpired, PinRequired exceptions
│   ├── appointment_service.py     # Slot computation, patient upsert, conflict detection
│   ├── visit_service.py           # Queue operations: check_in, call_next, done, skip, reorder
│   ├── notification_service.py    # WhatsApp message builders + Twilio send + DB logging
│   ├── payment_service.py         # Razorpay order creation + HMAC signature verification
│   ├── bill_pdf_service.py        # PDF generation with fpdf2 (on-demand, no file storage)
│   ├── invite_service.py          # Staff invite emails via SMTP
│   └── scheduler_service.py       # APScheduler: 24h reminder, 2h reminder, auto no-show
│
├── templates/                     # 40+ Jinja2 HTML templates
│   ├── base.html                  # Master layout — navbar, 7-item dock, PIN overlay, theme
│   ├── dashboard.html             # Stats cards, today's appointments, quick actions
│   ├── appointments.html          # Live queue + scheduled appointments
│   ├── patients.html              # Patient list with search
│   ├── patient_detail.html        # Full patient profile
│   ├── patient_vault.html         # Document vault per patient
│   ├── income.html                # Revenue dashboard with charts
│   ├── expenses.html              # Expense tracker
│   ├── settings.html              # Doctor settings — schedule, profile, PIN, subscription
│   ├── billing.html               # Subscription plan management
│   ├── pricing.html               # Plan comparison and purchase
│   ├── reports.html               # Analytics and reports
│   ├── calendar.html              # Monthly calendar view
│   ├── landing.html               # Public landing page
│   ├── login.html                 # Login form
│   ├── register.html              # Registration form
│   ├── bill_detail.html           # Bill view with PDF download
│   ├── bill_edit.html             # Edit existing bill
│   ├── public_booking.html        # Patient-facing booking page
│   ├── queue_display.html         # TV display for waiting room
│   ├── clinic/                    # Clinic admin templates
│   │   ├── admin_dashboard.html
│   │   ├── admin_doctors.html
│   │   ├── doctor_invite.html
│   │   └── admin_auth.html
│   └── ...
│
├── static/
│   └── css/main.css               # Design system — dark/light themes, parchment palette
│
├── uploads/                       # Patient files (gitignored)
│   └── patients/{doctor_id}/{patient_id}/
│
├── tests/
│   ├── conftest.py                # Pytest fixtures — test DB, test client
│   └── test_comprehensive.py      # Integration tests
│
├── seed_demo.py                   # Seed script for demo data
└── docs/
    └── design-tokens.md           # Design system reference
```

---

## Local Setup

**Prerequisites:** Python 3.10+

```bash
# Clone and enter project
git clone <repo-url>
cd Nivora

# Create virtual environment
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Create .env file
cp .env.example .env              # Edit with your values (see below)

# Run the server
uvicorn main:app --reload
```

Open `http://127.0.0.1:8000` — redirects to login. Register a new doctor account to get started.

If port 8000 is in use:
```bash
kill $(lsof -ti:8000) && uvicorn main:app --reload
```

---

## Environment Variables

```env
# ── Required ──────────────────────────────────────────────
DATABASE_URL=sqlite:///./clinic.db          # SQLite for dev, PostgreSQL for prod
SECRET_KEY=replace-with-a-64-char-hex       # JWT signing key

# ── Optional ──────────────────────────────────────────────
ALGORITHM=HS256                             # JWT algorithm (default: HS256)
ACCESS_TOKEN_EXPIRE_MINUTES=1440            # Session duration (default: 24 hours)

# ── WhatsApp / SMS Notifications (Twilio) ─────────────────
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your_auth_token
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886  # Twilio sandbox number
TWILIO_SMS_FROM=                            # Optional SMS fallback number

# ── Payments (Razorpay) ──────────────────────────────────
RAZORPAY_KEY_ID=rzp_test_XXXXXXXXXXXXXX     # Test or live key
RAZORPAY_KEY_SECRET=XXXXXXXXXXXXXXXXXXXXXXXX

# ── Platform Admin ────────────────────────────────────────
ADMIN_EMAIL=your-email@example.com          # Email of the admin doctor account
```

Generate a secure `SECRET_KEY`:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Twilio and Razorpay are **optional** for local development. The app runs without them — notifications log as `failed` and the payment button shows a configuration error.

---

## Database

### Development
SQLite — zero setup, file-based (`clinic.db` created automatically on first run).

### Production
PostgreSQL on Railway. Set `DATABASE_URL` to the PostgreSQL connection string.

### Migrations
Schema migrations run automatically on every app startup via `create_tables()` and `_run_migrations()` in `database/connection.py`. The migration system:
- Uses `CREATE TABLE IF NOT EXISTS` for new tables
- Uses `ALTER TABLE ADD COLUMN` wrapped in try/except for new columns
- Auto-creates a Clinic entity for any doctor without one (backfill migration)
- Safe to re-run — all migrations are idempotent

### Models (18 total)

| Model | Table | Purpose |
|---|---|---|
| `Doctor` | `doctors` | Doctor accounts with plan, settings, PIN |
| `Patient` | `patients` | Patient records linked to a doctor |
| `Appointment` | `appointments` | Scheduled appointments with status tracking |
| `Visit` | `visits` | Queue entries — token number, status, timestamps |
| `Bill` | `bills` | Invoice with subtotal, discount, GST, payment mode |
| `BillItem` | `bill_items` | Line items on a bill |
| `PriceCatalog` | `price_catalog` | Preset service prices for quick billing |
| `Clinic` | `clinics` | Multi-doctor clinic entity |
| `ClinicDoctor` | `clinic_doctors` | Doctor-clinic junction (owner/associate role) |
| `ClinicDoctorInvite` | `clinic_doctor_invites` | One-time invite tokens |
| `DoctorSchedule` | `doctor_schedules` | Working hours per day of week |
| `BlockedDate` | `blocked_dates` | Holiday/blocked full days |
| `BlockedTime` | `blocked_times` | Blocked time ranges on specific dates |
| `PatientNote` | `patient_notes` | Clinical notes per patient |
| `NoteFile` | `note_files` | File attachments on notes |
| `PatientDocument` | `patient_documents` | Document vault files |
| `Subscription` | `subscriptions` | Payment history and plan records |
| `NotificationLog` | `notifications_log` | WhatsApp/SMS send log |
| `Expense` | `expenses` | Clinic expense entries |
| `RecurringExpense` | `recurring_expenses` | Monthly recurring expense rules |
| `PinnedPatient` | `pinned_patients` | Frequently accessed patients |

---

## Queue System

The core of Nivora is the live patient queue. Every patient interaction flows through it:

```
Walk-in / Check-in
       │
       ▼
   ┌────────┐     Call      ┌─────────┐     Done     ┌─────────────────┐     Bill     ┌──────┐
   │WAITING │ ──────────▶   │ SERVING │ ──────────▶   │BILLING PENDING  │ ─────────▶   │ DONE │
   └────────┘               └─────────┘               └─────────────────┘              └──────┘
       │                         │                                                         ▲
       ▼                         ▼                                                         │
   ┌─────────┐              ┌───────────┐                                    Close Free ───┘
   │ SKIPPED │              │ CANCELLED │
   └─────────┘              └───────────┘
```

| Concept | Description |
|---|---|
| **Token Number** | Auto-assigned per doctor per day, monotonically increasing |
| **Queue Position** | Mutable — changes on skip, emergency, or manual move |
| **Emergency** | Promotes a patient to position #1 in the waiting queue |
| **Skip** | Moves patient to the end of the queue |
| **Call Next** | Automatically called after marking current patient as Done |
| **Free/Close** | Closes visit with zero charge, bypasses billing modal |
| **Public Display** | `/queue/{slug}` — shows current serving token on a TV screen |

### Walk-in Flow
1. Receptionist enters patient phone → auto-detects returning patients
2. Patient is checked in → token assigned → appears in waiting queue
3. WhatsApp notification sent with queue position and estimated wait

### Scheduled Appointment Flow
1. Appointment booked (by doctor, patient, or receptionist)
2. Patient arrives → receptionist clicks "Check In" → enters live queue
3. Same flow as walk-in from this point

---

## Billing Flow

```
Visit marked Done
       │
       ▼
┌──────────────────┐
│ Collect Payment  │ ← Modal with line items, catalog quick-fill
│ Modal            │
│                  │
│ • Line items     │
│ • Discount       │
│ • GST            │
│ • Payment mode   │
└──────────────────┘
       │
       ▼
Bill record created → WhatsApp receipt sent → Visit marked DONE
       │
       ▼
PDF available on-demand at /bills/{id}/pdf
```

- **Price Catalog:** Doctors can preset their consultation fees, procedure costs, etc. These appear as quick-fill buttons in the billing modal.
- **Payment Modes:** Cash, UPI, Card, Insurance, Free, Partial
- **PDF Bills:** Generated on-the-fly using fpdf2 when the doctor clicks Download. Not stored on disk.
- **Edit Bills:** Bills can be edited after creation — items, discount, and payment mode can be updated.

---

## Notification System

Six notification types, all sent via WhatsApp (Twilio):

| # | Type | Trigger | Message Content |
|---|---|---|---|
| 1 | Appointment Confirmed | Booking created | Clinic name, date, time, duration, address |
| 2 | Follow-up Confirmed | Follow-up booked | Same as above + "bring previous reports" |
| 3 | 24h Reminder | Scheduler (T-24h) | "Reminder: appointment tomorrow at X" |
| 4 | 2h Reminder | Scheduler (T-2h) | "Reminder: appointment in 2 hours" |
| 5 | Walk-in Queued | Walk-in check-in | Token number, people ahead, estimated wait |
| 6 | Bill Receipt | Bill created/edited | Itemized bill with total, payment mode |

### How it works
- All sends go through `_send_with_fallback()` → Twilio WhatsApp API
- Every attempt is logged in `notifications_log` table with `sent` or `failed` status
- All notification calls are wrapped in `try/except` — **a failure never blocks any user action**
- Background scheduler runs every 15 minutes to check for due reminders
- Duplicate prevention: `reminder_24h_sent` and `reminder_2h_sent` flags on each appointment

---

## Subscription & Payments

### Plans

| Plan | Monthly Price | Doctors | Per Doctor |
|---|---|---|---|
| **Free Trial** | Free (14 days) | 1 | - |
| **Solo** | ₹599 | 1 | ₹599 |
| **Duo** | ₹699 | 2 | ₹350 |
| **Clinic** | ₹1,599 | 5 | ₹320 |
| **Hospital** | ₹2,499 | 15 | ₹167 |
| **Enterprise** | ₹3,999 | Unlimited | Contact us |

### Payment Flow
1. Doctor navigates to `/billing` (active subscriber) or `/pricing` (trial/expired)
2. Clicks Subscribe/Upgrade → `POST /billing/create-order` creates a Razorpay order
3. Razorpay checkout opens in-browser (UPI, cards, net banking, wallets)
4. On payment success → `POST /billing/verify` verifies HMAC-SHA256 signature
5. On verification:
   - `doctor.plan_type` updated
   - `doctor.plan_expires_at = now + 30 days`
   - `Subscription` record created
   - For multi-doctor plans: `clinic.plan_type = "clinic"` synced
6. Redirect to `/dashboard?upgraded=1` with success banner

### Upgrade Logic
- Always pay full price of the new plan
- Always get 30 days from today (no proration, no stacking)
- Lower plans are disabled on the billing page
- Clinic owners can't downgrade to Solo if they have multiple doctors

---

## Multi-Doctor Clinic

### How it works
1. Doctor registers → automatically creates a Clinic entity (1:1)
2. Owner upgrades to a multi-doctor plan (Duo, Clinic, Hospital, Enterprise)
3. Owner invites associate doctors via email from `/clinic/admin/doctors`
4. Associates accept the invite link → join the clinic
5. Associates' access is covered by the owner's subscription — no separate billing
6. Each doctor has their own patients, appointments, and queue

### Clinic Admin Features
- Aggregated dashboard — total patients, appointments, revenue across all doctors
- Doctor management — invite, view status, roles
- PIN-protected admin authentication
- Unified public booking page at `/book/clinic/{slug}` with doctor selection

### Data Isolation
- Patients belong to individual doctors (not shared across clinic by default)
- Each doctor has their own schedule, queue, and billing
- Clinic-level billing planned for future

---

## Security

### Authentication
- JWT tokens stored in HTTP-only cookies (not localStorage)
- Tokens expire after 24 hours (configurable)
- Password hashing with bcrypt (Passlib)

### PIN Protection
One doctor account is shared with the receptionist. Sensitive sections are locked behind a 6-digit PIN:

| PIN Required | No PIN Needed |
|---|---|
| Income & Revenue | Appointments & Queue |
| Reports & Analytics | Walk-in check-in |
| Billing & Subscription | Patient list |
| Settings | Calendar |
| Patient detail (notes, vault) | Public booking |

PIN session lasts 30 minutes. Wrong PIN shows error on a blur overlay.

### Rate Limiting
- Login: max 10 attempts per IP per 15 minutes (429 response after)
- Public booking: max 5 per phone number per 24 hours

### Security Headers
Applied to every response:
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `X-XSS-Protection: 1; mode=block`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Permissions-Policy: geolocation=(), microphone=(), camera=()`
- `Cache-Control: no-store` on all authenticated pages

### Plan Expiry
- Expired plans trigger `PlanExpired` exception → redirect to billing
- Associate doctors on an expired clinic plan see a "plan lapsed" page
- Grace handling via `plan_grace_until` column

---

## Public Booking

Patients book appointments without logging in or downloading an app.

### Doctor Booking Page
`/book/{doctor-slug}` — shows available slots, patient fills name + phone.

### Clinic Booking Page
`/book/clinic/{clinic-slug}` — shows all doctors in the clinic, patient selects one and picks a slot.

### Confirmation Page
After booking, shows:
- Appointment details (date, time, doctor, clinic address)
- Google Calendar "Add to Calendar" link
- WhatsApp confirmation is sent automatically

---

## Design System

Warm sepia/parchment palette with dark and light themes.

| Property | Dark Theme | Light Theme |
|---|---|---|
| Background | `#1a1612` | `#e6e0d7` |
| Cards | `#211d18` | `#f5f2ec` |
| Text | `#ede8e2` | `#1a1410` |
| Muted text | `#9a8f85` | `#7a6f65` |
| Accent | `#c9a96e` | `#8a6520` |
| Navbar/Dock | `#2e1e0c` | `#2e1e0c` (always dark) |

- **Fonts:** `Inter` for body text, `Playfair Display` for brand logo and page titles
- **Border radius:** 6px (xs), 8px (sm), 16px (default), 24px (lg)
- **Theme toggle** persisted in `localStorage`
- Full token reference in `docs/design-tokens.md`

---

## Deployment

### Production Stack

| Service | Role |
|---|---|
| **Railway.app** | App hosting (always-on container) |
| **Railway PostgreSQL** | Database |

### Deploy

1. Push code to GitHub (Railway auto-deploys on push)
2. Set environment variables in Railway dashboard:
   - `DATABASE_URL` (from Railway Postgres plugin → Connect tab)
   - `SECRET_KEY`
   - `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET`
   - `ADMIN_EMAIL`
3. Railway runs: `uvicorn main:app --host 0.0.0.0 --port $PORT`

### Custom Domain
1. Railway → Service → Settings → Custom Domain → Add domain
2. Add CNAME record at your domain registrar pointing to Railway's target
3. Railway auto-provisions SSL

---

## API Reference

### Auth
| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/login` | - | Login page |
| POST | `/login` | - | Authenticate (rate-limited) |
| GET | `/register` | - | Registration page |
| POST | `/register` | - | Create doctor account |
| GET | `/logout` | - | Clear session |
| GET | `/auth/check` | JWT | Session validity check |

### Dashboard & Settings
| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/dashboard` | JWT | Main dashboard |
| GET | `/doctors/settings` | JWT+PIN | Settings page |
| POST | `/doctors/settings/schedule` | JWT+PIN | Save working hours |
| POST | `/doctors/settings/account` | JWT+PIN | Update account details |
| POST | `/doctors/settings/profile` | JWT+PIN | Update clinic profile |
| POST | `/doctors/settings/pin` | JWT+PIN | Set/update PIN |
| POST | `/doctors/settings/block` | JWT+PIN | Block a date |
| POST | `/doctors/settings/blocktime` | JWT+PIN | Block a time range |
| GET | `/calendar` | JWT | Monthly calendar view |
| GET | `/reports` | JWT+PIN | Analytics page |

### Appointments
| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/appointments` | JWT | List + live queue |
| GET | `/appointments/new` | JWT | New appointment form |
| POST | `/appointments` | JWT | Create appointment |
| GET | `/appointments/slots` | JWT | Available slot times (JSON) |
| POST | `/appointments/walkin` | JWT | Quick walk-in booking |
| GET | `/appointments/{id}/card` | JWT | Appointment detail card |
| POST | `/appointments/{id}/status` | JWT | Update status |
| POST | `/appointments/{id}/follow-up` | JWT | Schedule follow-up |

### Visits (Queue)
| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/visits/today-view` | JWT | Today's queue page |
| POST | `/visits/check-in` | JWT | Walk-in check-in |
| POST | `/visits/check-in-appt/{id}` | JWT | Check in scheduled patient |
| POST | `/visits/{id}/call` | JWT | Call patient to serving |
| POST | `/visits/{id}/done` | JWT | Mark visit done |
| POST | `/visits/{id}/skip` | JWT | Skip to end of queue |
| POST | `/visits/{id}/emergency` | JWT | Promote to front |
| POST | `/visits/{id}/cancel` | JWT | Cancel visit |
| POST | `/visits/{id}/close-free` | JWT | Close with zero charge |
| GET | `/queue/{slug}` | - | Public queue display |

### Billing
| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/visits/{id}/bill-prefill` | JWT | Prefill data for bill modal (JSON) |
| POST | `/visits/{id}/bill` | JWT | Create bill |
| GET | `/bills/{id}` | JWT+PIN | Bill detail page |
| GET | `/bills/{id}/edit` | JWT+PIN | Edit bill form |
| POST | `/bills/{id}/edit` | JWT+PIN | Update bill |
| POST | `/bills/{id}/mark-paid` | JWT+PIN | Mark as paid |
| GET | `/bills/{id}/pdf` | JWT+PIN | Download PDF |
| GET | `/price-catalog` | JWT | Get catalog items (JSON) |
| POST | `/price-catalog` | JWT | Add catalog item |
| POST | `/price-catalog/{id}/pin` | JWT | Toggle pin status |
| POST | `/price-catalog/{id}/delete` | JWT | Delete item |

### Patients
| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/patients` | JWT | Patient list |
| GET | `/patients/{id}` | JWT+PIN | Patient detail |
| POST | `/patients/{id}/edit` | JWT+PIN | Update patient |
| POST | `/patients/{id}/delete` | JWT+PIN | Delete patient |
| POST | `/patients/{id}/pin` | JWT | Pin patient |
| POST | `/patients/{id}/unpin` | JWT | Unpin patient |
| POST | `/patients/{id}/notes/add` | JWT+PIN | Add clinical note |
| GET | `/patients/{id}/vault` | JWT+PIN | Document vault |
| POST | `/patients/{id}/vault/upload` | JWT+PIN | Upload document |

### Income & Expenses
| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/income` | JWT+PIN | Income dashboard |
| GET | `/income/transactions` | JWT+PIN | Transaction history |
| GET | `/expenses` | JWT+PIN | Expense tracker |
| POST | `/expenses` | JWT+PIN | Add expense |
| POST | `/expenses/{id}/delete` | JWT+PIN | Delete expense |
| POST | `/expenses/recurring` | JWT+PIN | Add recurring rule |
| POST | `/expenses/recurring/{id}/toggle` | JWT+PIN | Toggle active |

### Subscription
| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/pricing` | JWT | Plan comparison page |
| GET | `/billing` | JWT+PIN | Current plan management |
| POST | `/billing/create-order` | JWT | Create Razorpay order (JSON) |
| POST | `/billing/verify` | JWT | Verify payment signature |

### Clinic Admin
| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/clinic/admin` | JWT+PIN | Clinic dashboard |
| GET | `/clinic/admin/doctors` | JWT+PIN | Manage doctors |
| POST | `/clinic/admin/doctors/invite` | JWT+PIN | Send invite |
| GET | `/doctor-invite/{token}` | - | Accept invite page |
| POST | `/doctor-invite/{token}` | - | Accept invite |

### Public Booking (No Auth)
| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/book/{slug}` | - | Doctor booking page |
| GET | `/book/{slug}/slots` | - | Available slots (JSON) |
| POST | `/book/{slug}` | - | Book appointment |
| GET | `/book/{slug}/confirm/{id}` | - | Confirmation page |
| GET | `/book/clinic/{slug}` | - | Clinic booking page |

### Platform Admin
| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/admin/dashboard` | Admin | Platform overview |
| GET | `/admin/doctors` | Admin | All doctors list |

---

## License

Private — all rights reserved.
