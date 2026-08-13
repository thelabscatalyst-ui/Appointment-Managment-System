# Nivora — Roadmap

Working document for feature-by-feature execution. Reflects verified repo state as of **2026-08-13**.

**Current state:** live at [nivora.store](https://www.nivora.store) on Railway + PostgreSQL · 108 tests passing · 130 routes · 13 routers.

---

## Legend

| Tier | Meaning |
|---|---|
| **P0** | Broken or blocking revenue/trust. Fix before building anything new. |
| **P1** | Highest-value next features. |
| **P2** | Compliance, scale, and multi-doctor depth. |
| **P3** | Nice-to-have / later. |

---

## P0 — Broken, blocking

These are not "features to build" — they are things users can already see failing.

### 1. WhatsApp delivery is dead in production
Twilio production credentials return `HTTP 401 — Authentication Error: invalid username` (confirmed in Railway logs). Every notification code path works — sends are dispatched via `BackgroundTasks`, logged to `notifications_log`, and the feedback link generates correctly — but **nothing reaches a patient**.

This silently breaks the product's headline promise (confirmations, reminders, receipts).

*Fix:* validate the Twilio SID/token pair in Railway env vars, confirm the WhatsApp sender is approved for the account, then verify a real send end-to-end and check `notifications_log` shows `status='sent'`.

### 2. Razorpay is on test keys
Checkout now opens correctly (the CSP fix landed), but the account is still in test mode — **no real money can be collected**. KYC is pending.

*Fix:* complete KYC → swap `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` in Railway. No code change needed.

### 3. Support contact is a placeholder
`templates/plan_lapsed.html` links to `wa.me/919999999999` — a fake number shown to doctors **at the exact moment their plan lapses and they want to pay**.

*Fix:* one-line replacement with the real support number.

### 4. Patient vault files are on ephemeral disk
Uploads live in `uploads/` on Railway's container filesystem and are **lost on every redeploy**. Doctors are storing lab reports and x-rays there.

*Fix:* migrate to Cloudflare R2 or S3. This is a data-loss bug, not an enhancement.

---

## P1 — Highest-value next features

### 5. Receptionist / staff role
Currently **scaffolding only**, and worth knowing exactly how incomplete:
- `request.state.is_staff` is *read* in ~8 places in `routers/appointments.py`
- It is **never assigned anywhere**
- There is **no `Staff` or `ClinicStaff` model**
- There is **no `/clinic/reception` route** and no reception template — every `is_staff` redirect points at a 404

So the branching logic exists but the entire feature behind it does not. This is the single biggest unlock for multi-doctor clinics — the receptionist is the person actually at the desk all day.

*Scope:* staff model + invite flow, auth dependency that sets `request.state.is_staff`, a reception workspace route/template, and permission gating (staff should see the queue and bookings, not income or settings).

### 6. Bulk patient import (CSV)
The #1 onboarding blocker for a clinic switching from paper or another system. Without it a doctor with 800 existing patients has to start empty.

*Scope:* CSV upload, column mapping UI, dry-run preview with per-row validation, dedupe on phone, partial-success reporting.

### 7. WhatsApp appointment reminders — verify end-to-end
Blocked on P0-1. The scheduler, templates, and dedupe flags (`reminder_24h_sent` / `reminder_2h_sent`) already exist; this is verification rather than construction once credentials work.

---

## P2 — Compliance & scale

### 8. DPDP consent flow
India's Digital Personal Data Protection Act. Today there is only a **code comment** in `database/models.py` (~line 216) noting PHI is stored in plaintext — no consent capture, no data-export, no deletion request path.

Patient WhatsApp consent (`wa_consent`, opt-out default) exists and is a reasonable starting primitive, but it is not a DPDP compliance story.

### 9. Audit log
**Not built at all.** For multi-doctor clinics, there is currently no record of who viewed or edited a patient record. Needed before selling into larger clinics.

### 10. Field-level encryption for PHI
Patient data is plaintext at rest. The model comment already flags this as a known roadmap item.

---

## P3 — Later

- Doctor-facing analytics beyond current reports (retention cohorts, revenue per procedure)
- Multi-language patient messaging (`language_pref` is captured but unused in sends)
- Inventory / pharmacy stock
- Lab integration
- Native mobile wrapper (the responsive web app covers phones and tablets well today)

---

## Recently shipped

Context for what has just landed, so we don't re-litigate it.

| Area | What shipped |
|---|---|
| **Branding** | Full rename ClinicOS → Nivora across 43 templates, `main.py`, `config.py`, README; domain moved to `nivora.store` |
| **Queue** | Hold/Resume — park a patient mid-consult and auto-call the next; resume to front of queue |
| **Feedback** | Tokenised public rating page, feedback link appended to WhatsApp receipts, ★ badge in patients list |
| **Mobile/Tablet** | Phone bottom nav, two-row queue cards, 767px breakpoint so tablets keep the desktop UI; verified at 375/768/1024px |
| **Payments** | Razorpay checkout fixed — CSP was blocking `checkout.razorpay.com` and `cdn.razorpay.com` |
| **Performance** | Notifications moved to `BackgroundTasks`; N+1 queries fixed with `joinedload`; reports counts collapsed 3 queries → 1; composite indexes added |
| **Data integrity** | Cancelling a visit now syncs `Appointment.status`, plus a backfill migration for previously-stuck rows |
| **Prescriptions** | e-Prescription module with 787-drug autocomplete, print layout, completeness warnings |

---

## Housekeeping

- **`./Clincos/` is a duplicate checkout** of this repo sitting inside the working tree, with its own `.git` and `.env`. The secrets themselves are safe — that `.env` is covered by the nested repo's own `.gitignore` — but the directory is untracked, so `git add -A` would add it as an **embedded git repository** (a gitlink), which is confusing and easy to commit by accident. Now gitignored; delete it when convenient.
- `tests/` emits ~850 deprecation warnings (`datetime.utcnow()`). Harmless today, will break on a future Python. Worth a sweep.
