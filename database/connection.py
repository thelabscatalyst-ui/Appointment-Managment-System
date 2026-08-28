import logging

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from config import settings

# Rewrite URL for correct SQLAlchemy driver
_db_url = settings.DATABASE_URL
_is_sqlite = "sqlite" in _db_url
if not _is_sqlite:
    # Railway provides postgresql:// or postgres:// — rewrite to pg8000 driver
    _db_url = _db_url.replace("postgresql://", "postgresql+pg8000://", 1)
    _db_url = _db_url.replace("postgres://", "postgresql+pg8000://", 1)

engine = create_engine(
    _db_url,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

logger = logging.getLogger(__name__)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_tables():
    from database import models  # noqa: F401 — ensures models are registered
    Base.metadata.create_all(bind=engine)
    _run_migrations()
    # Last line of defence: close any gap between what the models declare and
    # what the database actually has. See _reconcile_model_columns.
    _reconcile_model_columns()


def _run_migrations():
    """Apply additive schema migrations that create_all() won't handle."""
    import re as _re
    from sqlalchemy import text

    def _add_column(conn, sql):
        """Run ALTER TABLE … ADD COLUMN, silently ignore if column exists.

        Rolls back on failure so PostgreSQL doesn't leave the transaction in an
        aborted state (which would break every subsequent statement)."""
        try:
            conn.execute(text(sql))
            conn.commit()
        except Exception:
            conn.rollback()

    def _safe_ddl(conn, sql):
        """Run legacy SQLite-flavoured DDL, ignore failures, roll back cleanly.

        On a fresh PostgreSQL DB every table already exists (created by the ORM
        via create_all), so these CREATE statements are redundant and raise a
        syntax error on AUTOINCREMENT — caught here so migration continues."""
        try:
            conn.execute(text(sql))
            conn.commit()
        except Exception:
            conn.rollback()

    def _safe_exec(conn, sql, params=None):
        """Run a DML statement, ignore failures, roll back cleanly.

        Same contract as the two helpers above but for UPDATE/INSERT rather
        than DDL. The rollback matters most on PostgreSQL: an unguarded
        failure leaves the transaction aborted, which turns every subsequent
        statement in this function into an error too."""
        try:
            conn.execute(text(sql), params or {})
            conn.commit()
        except Exception:
            conn.rollback()

    with engine.connect() as conn:
        # ── Tier 1 ──────────────────────────────────────────────────────────
        _add_column(conn, "ALTER TABLE doctors ADD COLUMN pin_hash VARCHAR(255)")

        # ── Phase 2: nullable clinic/staff FK columns ────────────────────────
        _add_column(conn, "ALTER TABLE patients ADD COLUMN clinic_id INTEGER")
        _add_column(conn, "ALTER TABLE appointments ADD COLUMN clinic_id INTEGER")
        _add_column(conn, "ALTER TABLE appointments ADD COLUMN staff_id INTEGER")
        _add_column(conn, "ALTER TABLE doctor_schedules ADD COLUMN clinic_id INTEGER")
        _add_column(conn, "ALTER TABLE subscriptions ADD COLUMN clinic_id INTEGER")

        # ── Phase 2: auto-create implicit clinic for every existing doctor ───
        # For each doctor that has no clinic_doctors entry, create a Clinic row
        # and a ClinicDoctor row (role=owner), then backfill clinic_id on child
        # tables.  Safe to re-run: we check for existing clinic_doctors rows.
        # On a fresh PostgreSQL DB there are no doctors yet, so this is a no-op.
        try:
            doctors_without_clinic = conn.execute(text(
                "SELECT d.id, d.clinic_name, d.clinic_address, d.city, d.slug "
                "FROM doctors d "
                "WHERE NOT EXISTS ("
                "  SELECT 1 FROM clinic_doctors cd WHERE cd.doctor_id = d.id"
                ")"
            )).fetchall()
        except Exception:
            conn.rollback()
            doctors_without_clinic = []

        for row in doctors_without_clinic:
            doctor_id   = row[0]
            clinic_name = row[1] or "My Clinic"
            address     = row[2]
            city        = row[3]
            base_slug   = (row[4] or f"clinic-{doctor_id}") + "-clinic"
            # ensure slug uniqueness
            slug = base_slug
            counter = 1
            while conn.execute(
                text("SELECT 1 FROM clinics WHERE slug = :s"), {"s": slug}
            ).fetchone():
                slug = f"{base_slug}-{counter}"
                counter += 1

            # Insert clinic
            conn.execute(text(
                "INSERT INTO clinics (name, address, city, slug, plan_type, owner_doctor_id, created_at) "
                "VALUES (:name, :addr, :city, :slug, 'trial', :owner, CURRENT_TIMESTAMP)"
            ), {"name": clinic_name, "addr": address, "city": city,
                "slug": slug, "owner": doctor_id})
            conn.commit()

            clinic_id = conn.execute(text(
                "SELECT id FROM clinics WHERE slug = :s"), {"s": slug}
            ).fetchone()[0]

            # Insert clinic_doctors (owner)
            conn.execute(text(
                "INSERT INTO clinic_doctors (clinic_id, doctor_id, role, is_active, joined_at) "
                "VALUES (:cid, :did, 'owner', 1, CURRENT_TIMESTAMP)"
            ), {"cid": clinic_id, "did": doctor_id})
            conn.commit()

            # Backfill clinic_id on child tables for this doctor
            conn.execute(text(
                "UPDATE patients SET clinic_id = :cid "
                "WHERE doctor_id = :did AND clinic_id IS NULL"
            ), {"cid": clinic_id, "did": doctor_id})
            conn.execute(text(
                "UPDATE appointments SET clinic_id = :cid "
                "WHERE doctor_id = :did AND clinic_id IS NULL"
            ), {"cid": clinic_id, "did": doctor_id})
            conn.execute(text(
                "UPDATE doctor_schedules SET clinic_id = :cid "
                "WHERE doctor_id = :did AND clinic_id IS NULL"
            ), {"cid": clinic_id, "did": doctor_id})
            conn.commit()

        # ── Phase 4: Walk-in buffer + emergency flag ─────────────────────────
        _add_column(conn, "ALTER TABLE doctor_schedules ADD COLUMN walk_in_buffer INTEGER DEFAULT 0")
        _add_column(conn, "ALTER TABLE appointments ADD COLUMN is_emergency BOOLEAN DEFAULT FALSE")

        # ── Phase 3: Patient notes & file attachments ────────────────────────
        _safe_ddl(conn,
            "CREATE TABLE IF NOT EXISTS patient_notes ("
            "  id         INTEGER PRIMARY KEY AUTOINCREMENT, "
            "  patient_id INTEGER NOT NULL REFERENCES patients(id), "
            "  doctor_id  INTEGER NOT NULL REFERENCES doctors(id), "
            "  note_text  TEXT    NOT NULL, "
            "  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
        _safe_ddl(conn,
            "CREATE TABLE IF NOT EXISTS note_files ("
            "  id            INTEGER PRIMARY KEY AUTOINCREMENT, "
            "  note_id       INTEGER NOT NULL REFERENCES patient_notes(id), "
            "  original_name VARCHAR(255) NOT NULL, "
            "  stored_name   VARCHAR(255) NOT NULL, "
            "  file_size     INTEGER, "
            "  uploaded_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
        # ── Blocked time ranges ──────────────────────────────────────────────
        _safe_ddl(conn,
            "CREATE TABLE IF NOT EXISTS blocked_times ("
            "  id           INTEGER PRIMARY KEY AUTOINCREMENT, "
            "  doctor_id    INTEGER NOT NULL REFERENCES doctors(id), "
            "  blocked_date DATE    NOT NULL, "
            "  start_time   TIME    NOT NULL, "
            "  end_time     TIME    NOT NULL, "
            "  reason       VARCHAR(200)"
            ")"
        )
        # ── Patient age / gender ─────────────────────────────────────────────
        _add_column(conn, "ALTER TABLE patients ADD COLUMN age INTEGER")
        _add_column(conn, "ALTER TABLE patients ADD COLUMN gender VARCHAR(10)")

        # ── Pinned patients ──────────────────────────────────────────────────
        _safe_ddl(conn,
            "CREATE TABLE IF NOT EXISTS pinned_patients ("
            "  id         INTEGER PRIMARY KEY AUTOINCREMENT, "
            "  doctor_id  INTEGER NOT NULL REFERENCES doctors(id), "
            "  patient_id INTEGER NOT NULL REFERENCES patients(id), "
            "  pinned_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
        conn.commit()

        # ── v2: doctor mode + walk-in policy ─────────────────────────────────
        _add_column(conn, "ALTER TABLE doctors ADD COLUMN doctor_mode VARCHAR(30) DEFAULT 'reception_driven'")
        _add_column(conn, "ALTER TABLE doctors ADD COLUMN walkin_policy VARCHAR(20) DEFAULT 'booked_jumps'")

        # ── v2: appointment → visit link ──────────────────────────────────────
        _add_column(conn, "ALTER TABLE appointments ADD COLUMN visit_id INTEGER")
        _add_column(conn, "ALTER TABLE appointments ADD COLUMN arrival_status VARCHAR(20)")

        # ── v2: visits table ──────────────────────────────────────────────────
        _safe_ddl(conn,
            "CREATE TABLE IF NOT EXISTS visits ("
            "  id             INTEGER PRIMARY KEY AUTOINCREMENT, "
            "  doctor_id      INTEGER NOT NULL REFERENCES doctors(id), "
            "  patient_id     INTEGER NOT NULL REFERENCES patients(id), "
            "  clinic_id      INTEGER REFERENCES clinics(id), "
            "  appointment_id INTEGER REFERENCES appointments(id), "
            "  visit_date     DATE    NOT NULL, "
            "  token_number   INTEGER NOT NULL, "
            "  queue_position INTEGER, "
            "  status         VARCHAR(20) NOT NULL DEFAULT 'waiting', "
            "  is_emergency   BOOLEAN NOT NULL DEFAULT 0, "
            "  source         VARCHAR(20) NOT NULL DEFAULT 'walk_in', "
            "  check_in_time  TIMESTAMP, "
            "  call_time      TIMESTAMP, "
            "  complete_time  TIMESTAMP, "
            "  bill_id        INTEGER, "
            "  notes          TEXT, "
            "  created_by     INTEGER, "
            "  UNIQUE(doctor_id, visit_date, token_number)"
            ")"
        )
        conn.commit()

        # ── v2: bills + bill_items ────────────────────────────────────────────
        _safe_ddl(conn,
            "CREATE TABLE IF NOT EXISTS bills ("
            "  id           INTEGER PRIMARY KEY AUTOINCREMENT, "
            "  visit_id     INTEGER UNIQUE NOT NULL REFERENCES visits(id), "
            "  doctor_id    INTEGER NOT NULL REFERENCES doctors(id), "
            "  clinic_id    INTEGER REFERENCES clinics(id), "
            "  patient_id   INTEGER NOT NULL REFERENCES patients(id), "
            "  subtotal     NUMERIC(10,2) DEFAULT 0, "
            "  discount     NUMERIC(10,2) DEFAULT 0, "
            "  gst_amount   NUMERIC(10,2) DEFAULT 0, "
            "  total        NUMERIC(10,2) DEFAULT 0, "
            "  paid_amount  NUMERIC(10,2) DEFAULT 0, "
            "  payment_mode VARCHAR(20), "
            "  paid_at      TIMESTAMP, "
            "  notes        TEXT, "
            "  created_by   INTEGER, "
            "  created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
        _safe_ddl(conn,
            "CREATE TABLE IF NOT EXISTS bill_items ("
            "  id          INTEGER PRIMARY KEY AUTOINCREMENT, "
            "  bill_id     INTEGER NOT NULL REFERENCES bills(id), "
            "  description VARCHAR(200) NOT NULL, "
            "  category    VARCHAR(50), "
            "  quantity    INTEGER NOT NULL DEFAULT 1, "
            "  unit_price  NUMERIC(10,2) NOT NULL, "
            "  total       NUMERIC(10,2) NOT NULL, "
            "  gst_rate    NUMERIC(4,2)  DEFAULT 0"
            ")"
        )
        conn.commit()

        # ── v2: price catalog ─────────────────────────────────────────────────
        _safe_ddl(conn,
            "CREATE TABLE IF NOT EXISTS price_catalog ("
            "  id            INTEGER PRIMARY KEY AUTOINCREMENT, "
            "  doctor_id     INTEGER NOT NULL REFERENCES doctors(id), "
            "  clinic_id     INTEGER REFERENCES clinics(id), "
            "  name          VARCHAR(100) NOT NULL, "
            "  category      VARCHAR(50), "
            "  default_price NUMERIC(10,2) NOT NULL, "
            "  is_pinned     BOOLEAN NOT NULL DEFAULT 0, "
            "  sort_order    INTEGER NOT NULL DEFAULT 0, "
            "  is_active     BOOLEAN NOT NULL DEFAULT 1, "
            "  created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
        conn.commit()

        # ── v2: expenses + recurring expenses ─────────────────────────────────
        _safe_ddl(conn,
            "CREATE TABLE IF NOT EXISTS recurring_expenses ("
            "  id           INTEGER PRIMARY KEY AUTOINCREMENT, "
            "  doctor_id    INTEGER NOT NULL REFERENCES doctors(id), "
            "  clinic_id    INTEGER REFERENCES clinics(id), "
            "  category     VARCHAR(30) NOT NULL, "
            "  amount       NUMERIC(10,2) NOT NULL, "
            "  label        VARCHAR(100) NOT NULL, "
            "  day_of_month INTEGER NOT NULL, "
            "  is_active    BOOLEAN NOT NULL DEFAULT 1, "
            "  created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
        _safe_ddl(conn,
            "CREATE TABLE IF NOT EXISTS expenses ("
            "  id           INTEGER PRIMARY KEY AUTOINCREMENT, "
            "  doctor_id    INTEGER NOT NULL REFERENCES doctors(id), "
            "  clinic_id    INTEGER REFERENCES clinics(id), "
            "  category     VARCHAR(30) NOT NULL, "
            "  amount       NUMERIC(10,2) NOT NULL, "
            "  expense_date DATE NOT NULL, "
            "  description  VARCHAR(300), "
            "  recurring_id INTEGER REFERENCES recurring_expenses(id), "
            "  created_by   INTEGER, "
            "  created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
        conn.commit()

        # ── appointment card — new patient + appointment fields ───────────────
        _add_column(conn, "ALTER TABLE patients ADD COLUMN blood_group VARCHAR(10)")
        _add_column(conn, "ALTER TABLE patients ADD COLUMN allergies TEXT")
        _add_column(conn, "ALTER TABLE patients ADD COLUMN preferred_contact VARCHAR(20) DEFAULT 'phone'")
        _add_column(conn, "ALTER TABLE appointments ADD COLUMN reception_notes TEXT")
        _add_column(conn, "ALTER TABLE appointments ADD COLUMN follow_up_date DATE")
        conn.commit()

        # ── v3: clinic plan management columns ───────────────────────────────
        # DATETIME is a SQLite type; PostgreSQL has no such type and rejects
        # the statement, which _add_column then swallows — so on Postgres this
        # column silently never existed. That was invisible until the Clinic
        # model started declaring plan_grace_until, at which point every
        # db.query(Clinic) emitted "SELECT clinics.plan_grace_until" and blew
        # up with UndefinedColumn, 500-ing login for anyone with a clinic.
        _add_column(conn,
            "ALTER TABLE clinics ADD COLUMN plan_grace_until "
            + ("DATETIME" if _is_sqlite else "TIMESTAMP"))
        _add_column(conn, "ALTER TABLE clinics ADD COLUMN max_doctors INTEGER DEFAULT 1")
        _add_column(conn, "ALTER TABLE clinics ADD COLUMN max_staff INTEGER DEFAULT 2")
        _add_column(conn, "ALTER TABLE clinics ADD COLUMN billing_access_staff BOOLEAN DEFAULT FALSE")
        # Backfill: solo-plan clinics keep max_doctors=1; set Clinic plan ones to 5
        # Guarded: this ran unwrapped, so on PostgreSQL a single failure here
        # aborted the transaction and poisoned every statement below it.
        _safe_exec(conn,
            "UPDATE clinics SET max_doctors = 5, max_staff = 999 "
            "WHERE plan_type = 'clinic'"
        )

        # ── patient document vault ────────────────────────────────────────────
        _safe_ddl(conn,
            "CREATE TABLE IF NOT EXISTS patient_documents ("
            "  id            INTEGER PRIMARY KEY AUTOINCREMENT, "
            "  doctor_id     INTEGER NOT NULL REFERENCES doctors(id), "
            "  patient_id    INTEGER NOT NULL REFERENCES patients(id), "
            "  original_name VARCHAR(255) NOT NULL, "
            "  stored_name   VARCHAR(255) NOT NULL, "
            "  file_size     INTEGER NOT NULL, "
            "  mime_type     VARCHAR(100), "
            "  category      VARCHAR(50) DEFAULT 'other', "
            "  description   TEXT, "
            "  uploaded_at   DATETIME DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
        conn.commit()

        # ── Notification system: avg consult time per doctor ─────────────────
        _add_column(conn, "ALTER TABLE doctors ADD COLUMN avg_consult_mins INTEGER DEFAULT 10")

        # ── Seat-based billing: max doctors per plan ──────────────────────────
        _add_column(conn, "ALTER TABLE doctors ADD COLUMN plan_seats INTEGER")

        # ── Marketing source tracking on patients (first-touch attribution) ──
        _add_column(conn, "ALTER TABLE patients ADD COLUMN referral_source VARCHAR(30)")
        _add_column(conn, "ALTER TABLE patients ADD COLUMN referral_source_other VARCHAR(120)")

        # ── Make notifications_log.appointment_id nullable ───────────────────
        # SQLite doesn't support ALTER COLUMN, so recreate the table.
        # On PostgreSQL the ORM creates it correctly — skip this block.
        has_nullable = ""
        if _is_sqlite:
            has_nullable = conn.execute(text(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='notifications_log'"
            )).scalar() or ""
        if "appointment_id INTEGER NOT NULL" in has_nullable:
            conn.execute(text("ALTER TABLE notifications_log RENAME TO notifications_log_old"))
            _safe_ddl(conn,
                "CREATE TABLE notifications_log ("
                "  id             INTEGER PRIMARY KEY AUTOINCREMENT, "
                "  appointment_id INTEGER REFERENCES appointments(id), "
                "  type           VARCHAR(30), "
                "  channel        VARCHAR(20), "
                "  message_body   TEXT, "
                "  status         VARCHAR(10), "
                "  sent_at        TIMESTAMP"
                ")"
            )
            conn.execute(text(
                "INSERT INTO notifications_log SELECT * FROM notifications_log_old"
            ))
            conn.execute(text("DROP TABLE notifications_log_old"))
            conn.commit()

        # ── v3: Doctor medical registration number + platform verification ────
        _add_column(conn, "ALTER TABLE doctors ADD COLUMN medical_reg_number VARCHAR(50)")
        _add_column(conn, "ALTER TABLE doctors ADD COLUMN is_verified BOOLEAN DEFAULT FALSE")

        # ── v3: Patient WhatsApp consent ──────────────────────────────────────
        _add_column(conn, "ALTER TABLE patients ADD COLUMN wa_consent BOOLEAN DEFAULT FALSE")
        _add_column(conn, "ALTER TABLE patients ADD COLUMN wa_consent_at TIMESTAMP")

        # ── v3: e-Prescription tables ─────────────────────────────────────────
        _safe_ddl(conn,
            "CREATE TABLE IF NOT EXISTS prescriptions ("
            "  id         INTEGER PRIMARY KEY AUTOINCREMENT, "
            "  doctor_id  INTEGER NOT NULL REFERENCES doctors(id), "
            "  patient_id INTEGER NOT NULL REFERENCES patients(id), "
            "  visit_id   INTEGER REFERENCES visits(id), "
            "  diagnosis  TEXT, "
            "  advice     TEXT, "
            "  follow_up  VARCHAR(100), "
            "  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
            "  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
        _safe_ddl(conn,
            "CREATE TABLE IF NOT EXISTS prescription_items ("
            "  id              INTEGER PRIMARY KEY AUTOINCREMENT, "
            "  prescription_id INTEGER NOT NULL REFERENCES prescriptions(id), "
            "  drug_name       VARCHAR(150) NOT NULL, "
            "  dosage          VARCHAR(80), "
            "  frequency       VARCHAR(80), "
            "  duration        VARCHAR(60), "
            "  instructions    VARCHAR(200)"
            ")"
        )
        conn.commit()

        # ── Test account: keep arjunmehta@clinic.com on a rolling 30-day trial ─
        try:
            from datetime import datetime, timedelta
            _new_expiry = datetime.utcnow() + timedelta(days=30)
            conn.execute(text(
                "UPDATE doctors SET trial_ends_at = :exp, plan_type = 'trial' "
                "WHERE email = 'arjunmehta@clinic.com'"
            ), {"exp": _new_expiry})
            conn.commit()
        except Exception:
            conn.rollback()

        # ── Owner account: keep thelabscatalyst@gmail.com on the Clinic tier ──
        # "Clinic Account" at signup did nothing until the account_type field
        # was actually read, so every account created before that — including
        # this one — has plan_type='trial' on its clinic and therefore sees no
        # Clinic Admin. Nothing recorded which historical accounts chose the
        # clinic option, so this cannot be a blanket migration without also
        # promoting genuine solo doctors; it is scoped to the one account.
        #
        # Rolling, like the arjunmehta test-account block above, so the tier
        # does not silently lapse mid-testing. Idempotent: safe on every boot.
        try:
            from datetime import datetime as _dt2, timedelta as _td2
            _owner_email = "thelabscatalyst@gmail.com"
            _end = _dt2.utcnow() + _td2(days=30)
            _row = conn.execute(text(
                "SELECT cl.id FROM clinics cl "
                "JOIN clinic_doctors cd ON cd.clinic_id = cl.id "
                "JOIN doctors d ON d.id = cd.doctor_id "
                "WHERE d.email = :em AND cd.role = 'owner'"
            ), {"em": _owner_email}).fetchone()
            if _row:
                conn.execute(text(
                    "UPDATE clinics SET plan_type = 'clinic', plan_expires_at = :end, "
                    "plan_grace_until = :grace, max_doctors = 5 WHERE id = :cid"
                ), {"end": _end, "grace": _end + _td2(days=7), "cid": _row[0]})
                # Their own trial had already lapsed; refresh it so the account
                # is usable rather than bouncing to /billing.
                conn.execute(text(
                    "UPDATE doctors SET trial_ends_at = :end WHERE email = :em"
                ), {"end": _end, "em": _owner_email})
                conn.commit()
        except Exception:
            conn.rollback()

        # ── Hold feature: add 'on_hold' to the visitstatus enum ──────────────
        # PostgreSQL uses a native enum type; a new Python enum member must be
        # added to the DB type or inserting it raises. No-op on SQLite (VARCHAR).
        if not _is_sqlite:
            try:
                conn.execute(text(
                    "ALTER TYPE visitstatus ADD VALUE IF NOT EXISTS 'on_hold'"
                ))
                conn.commit()
            except Exception:
                conn.rollback()

        # ── v6: clinic_id attribution backfill ───────────────────────────────
        # clinic_id has existed on these tables for a long time but was never
        # populated on most write paths and never read by any query — it was
        # NULL on 100% of appointments, patients, schedules and price-catalog
        # rows. Multi-clinic scoping needs it to be truthful, so backfill it.
        #
        # Order matters: derive from a LINKED row wherever one exists, and only
        # then fall back to the doctor's home clinic. A bill in particular must
        # inherit its visit's clinic — attributing it to the doctor's own
        # clinic would move revenue between businesses.
        #
        # Every statement is guarded by `WHERE clinic_id IS NULL`, so re-running
        # migrations is a no-op. All SQL below is portable to SQLite and
        # PostgreSQL (correlated scalar subqueries, CASE, LIMIT).

        # 1. appointments <- their linked visit
        _safe_exec(conn,
            "UPDATE appointments SET clinic_id = ("
            "  SELECT v.clinic_id FROM visits v WHERE v.id = appointments.visit_id"
            ") "
            "WHERE clinic_id IS NULL AND visit_id IS NOT NULL "
            "  AND EXISTS (SELECT 1 FROM visits v2 "
            "              WHERE v2.id = appointments.visit_id "
            "                AND v2.clinic_id IS NOT NULL)"
        )

        # 2. visits <- their linked appointment
        _safe_exec(conn,
            "UPDATE visits SET clinic_id = ("
            "  SELECT a.clinic_id FROM appointments a WHERE a.id = visits.appointment_id"
            ") "
            "WHERE clinic_id IS NULL AND appointment_id IS NOT NULL "
            "  AND EXISTS (SELECT 1 FROM appointments a2 "
            "              WHERE a2.id = visits.appointment_id "
            "                AND a2.clinic_id IS NOT NULL)"
        )

        # 3. bills <- their visit (never the doctor's home clinic)
        _safe_exec(conn,
            "UPDATE bills SET clinic_id = ("
            "  SELECT v.clinic_id FROM visits v WHERE v.id = bills.visit_id"
            ") "
            "WHERE clinic_id IS NULL AND visit_id IS NOT NULL "
            "  AND EXISTS (SELECT 1 FROM visits v2 "
            "              WHERE v2.id = bills.visit_id "
            "                AND v2.clinic_id IS NOT NULL)"
        )

        # 4. expenses <- the recurring rule that generated them
        _safe_exec(conn,
            "UPDATE expenses SET clinic_id = ("
            "  SELECT r.clinic_id FROM recurring_expenses r WHERE r.id = expenses.recurring_id"
            ") "
            "WHERE clinic_id IS NULL AND recurring_id IS NOT NULL "
            "  AND EXISTS (SELECT 1 FROM recurring_expenses r2 "
            "              WHERE r2.id = expenses.recurring_id "
            "                AND r2.clinic_id IS NOT NULL)"
        )

        # 5. Home-clinic fallback for anything still NULL. Prefers the clinic
        #    the doctor OWNS, then lowest clinic_id, so the result is
        #    deterministic rather than insertion-ordered.
        #
        #    Known limitation: for a doctor who genuinely works at two clinics,
        #    this collapses their history onto the owned one. There is no
        #    recoverable signal for where past work actually happened, and
        #    guessing from timestamps would be worse than a consistent rule.
        for _tbl in (
            "appointments", "visits", "bills", "patients",
            "expenses", "recurring_expenses", "doctor_schedules", "price_catalog",
        ):
            _safe_exec(conn,
                f"UPDATE {_tbl} SET clinic_id = ("
                "  SELECT cd.clinic_id FROM clinic_doctors cd "
                f" WHERE cd.doctor_id = {_tbl}.doctor_id "
                "  ORDER BY CASE WHEN cd.role = 'owner' THEN 0 ELSE 1 END, cd.clinic_id "
                "  LIMIT 1"
                ") "
                "WHERE clinic_id IS NULL "
                "  AND EXISTS (SELECT 1 FROM clinic_doctors cd2 "
                f"             WHERE cd2.doctor_id = {_tbl}.doctor_id)"
            )

        # ── v6: token numbering becomes per (doctor, clinic, day) ────────────
        # The old constraint was (doctor_id, visit_date, token_number), so a
        # doctor working two clinics in one day shared a single token sequence.
        # Must run BEFORE the index loop below: on SQLite this rebuilds the
        # table, and DROP TABLE takes every ix_visits_* index with it.
        if _is_sqlite:
            _visits_ddl = conn.execute(text(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='visits'"
            )).scalar() or ""
            if _visits_ddl and "uq_visit_token_per_clinic_day" not in _visits_ddl:
                try:
                    # Columns are named explicitly rather than INSERT..SELECT *.
                    # The ORM order and the hand-written CREATE above currently
                    # agree, but they are maintained independently — a silent
                    # drift would scramble a clinical queue.
                    _cols = (
                        "id, doctor_id, patient_id, clinic_id, appointment_id, "
                        "visit_date, token_number, queue_position, status, "
                        "is_emergency, source, check_in_time, call_time, "
                        "complete_time, bill_id, notes, created_by"
                    )
                    conn.execute(text("ALTER TABLE visits RENAME TO visits_old"))
                    conn.execute(text(
                        "CREATE TABLE visits ("
                        "  id             INTEGER PRIMARY KEY AUTOINCREMENT, "
                        "  doctor_id      INTEGER NOT NULL REFERENCES doctors(id), "
                        "  patient_id     INTEGER NOT NULL REFERENCES patients(id), "
                        "  clinic_id      INTEGER REFERENCES clinics(id), "
                        "  appointment_id INTEGER REFERENCES appointments(id), "
                        "  visit_date     DATE    NOT NULL, "
                        "  token_number   INTEGER NOT NULL, "
                        "  queue_position INTEGER, "
                        "  status         VARCHAR(20) NOT NULL DEFAULT 'waiting', "
                        "  is_emergency   BOOLEAN NOT NULL DEFAULT 0, "
                        "  source         VARCHAR(20) NOT NULL DEFAULT 'walk_in', "
                        "  check_in_time  TIMESTAMP, "
                        "  call_time      TIMESTAMP, "
                        "  complete_time  TIMESTAMP, "
                        "  bill_id        INTEGER, "
                        "  notes          TEXT, "
                        "  created_by     INTEGER, "
                        "  CONSTRAINT uq_visit_token_per_clinic_day "
                        "    UNIQUE (doctor_id, clinic_id, visit_date, token_number)"
                        ")"
                    ))
                    conn.execute(text(
                        f"INSERT INTO visits ({_cols}) SELECT {_cols} FROM visits_old"
                    ))
                    conn.execute(text("DROP TABLE visits_old"))
                    # DROP TABLE took every index with it, including the
                    # single-column ones the ORM created via Column(index=True)
                    # — create_all already ran, so it will not recreate them.
                    # The composite loop below only covers its own two.
                    for _rix in (
                        "CREATE INDEX IF NOT EXISTS ix_visits_id ON visits (id)",
                        "CREATE INDEX IF NOT EXISTS ix_visits_doctor_id ON visits (doctor_id)",
                        "CREATE INDEX IF NOT EXISTS ix_visits_patient_id ON visits (patient_id)",
                        "CREATE INDEX IF NOT EXISTS ix_visits_clinic_id ON visits (clinic_id)",
                        "CREATE INDEX IF NOT EXISTS ix_visits_visit_date ON visits (visit_date)",
                        "CREATE INDEX IF NOT EXISTS ix_visits_status ON visits (status)",
                    ):
                        conn.execute(text(_rix))
                    conn.commit()
                except Exception:
                    conn.rollback()
        else:
            # PostgreSQL: a unique INDEX rather than a constraint, because
            # there is no ADD CONSTRAINT IF NOT EXISTS — the index form is what
            # makes this re-runnable.
            _safe_exec(conn,
                "ALTER TABLE visits DROP CONSTRAINT IF EXISTS uq_visit_token_per_doctor_day")
            _safe_exec(conn,
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_visit_token_per_clinic_day "
                "ON visits (doctor_id, clinic_id, visit_date, token_number)")

        # ── Performance: composite indexes on the hottest filter columns ─────
        # (doctor_id + date) covers the appointment-list and queue queries.
        # CREATE INDEX IF NOT EXISTS works on both SQLite and PostgreSQL.
        for _ix_sql in (
            "CREATE INDEX IF NOT EXISTS ix_appointments_doctor_date "
            "ON appointments (doctor_id, appointment_date)",
            "CREATE INDEX IF NOT EXISTS ix_visits_doctor_date "
            "ON visits (doctor_id, visit_date)",
            # Clinic-scoped reads land next; these cover them.
            "CREATE INDEX IF NOT EXISTS ix_visits_clinic_date "
            "ON visits (clinic_id, visit_date)",
            "CREATE INDEX IF NOT EXISTS ix_appointments_clinic_date "
            "ON appointments (clinic_id, appointment_date)",
            "CREATE INDEX IF NOT EXISTS ix_bills_clinic_paid "
            "ON bills (clinic_id, paid_at)",
        ):
            _add_column(conn, _ix_sql)

        # ── Backfill: appointments left stuck on 'scheduled' from before the
        # cancel_visit fix started syncing Appointment.status. Any appointment
        # whose linked visit was cancelled should show as cancelled too.
        try:
            conn.execute(text(
                "UPDATE appointments SET status = 'cancelled' "
                "WHERE status = 'scheduled' AND id IN ("
                "  SELECT appointment_id FROM visits "
                "  WHERE appointment_id IS NOT NULL AND status = 'cancelled'"
                ")"
            ))
            conn.commit()
        except Exception:
            conn.rollback()

        # ── v4: email ownership verification ─────────────────────────────────
        # TIMESTAMP (not BOOLEAN) — records when, and sidesteps the Postgres
        # "BOOLEAN DEFAULT 0" failure that silently drops the column.
        # The email_verifications TABLE needs no DDL here: create_all() above
        # builds it from the model class.
        _add_column(conn, "ALTER TABLE doctors ADD COLUMN email_verified_at TIMESTAMP")

        # Grandfather existing doctors. They registered before verification
        # existed; forcing them through it retroactively would strand real
        # users mid-clinic, and their accounts are already in active use.
        # New signups start NULL and must verify.
        try:
            conn.execute(text(
                "UPDATE doctors SET email_verified_at = created_at "
                "WHERE email_verified_at IS NULL"
            ))
            conn.commit()
        except Exception:
            conn.rollback()

        # ── v4: session invalidation on password reset ───────────────────────
        # INTEGER DEFAULT 0 is safe on both engines (the DEFAULT 0 trap is
        # BOOLEAN-specific). password_resets table comes from create_all().
        _add_column(conn, "ALTER TABLE doctors ADD COLUMN token_version INTEGER DEFAULT 0")
        try:
            conn.execute(text(
                "UPDATE doctors SET token_version = 0 WHERE token_version IS NULL"
            ))
            conn.commit()
        except Exception:
            conn.rollback()

        # ── v5: optional per-drug prescription notes ──────────────────────────
        _add_column(conn, "ALTER TABLE prescription_items ADD COLUMN notes TEXT")

        # The support board (support_queries / support_replies tables and the
        # doctors.is_admin column) was removed. Existing databases keep those
        # objects — nothing references them, and dropping data is not worth
        # the risk to reclaim a few unused columns.


def _reconcile_model_columns():
    """Add any column a model declares but the database lacks.

    Safety net for the failure mode that took production down: a hand-written
    ALTER TABLE used a type the engine rejects (DATETIME is SQLite-only;
    PostgreSQL wants TIMESTAMP), _add_column swallowed the error, and the
    column silently never existed. Nothing noticed until a model started
    declaring it — at which point every SELECT of that table named a column
    that wasn't there and returned a 500.

    Types come from the SQLAlchemy dialect here rather than being hand-written,
    so they are correct for whichever engine is actually running. Only ever
    ADDs nullable columns; never drops, alters or reorders anything.
    """
    from sqlalchemy import inspect as _inspect, text
    from database import models  # noqa: F401 — ensures models are registered

    inspector = _inspect(engine)
    try:
        existing_tables = set(inspector.get_table_names())
    except Exception:
        return

    with engine.connect() as conn:
        # tables.values(), not sorted_tables: we only ALTER existing tables, so
        # dependency order is irrelevant — and sorting warns (and may soon
        # raise) on the appointments/visits mutual FK cycle.
        for table in Base.metadata.tables.values():
            if table.name not in existing_tables:
                continue
            try:
                have = {c["name"] for c in inspector.get_columns(table.name)}
            except Exception:
                continue
            for col in table.columns:
                if col.name in have or col.primary_key:
                    continue
                try:
                    coltype = col.type.compile(dialect=engine.dialect)
                    conn.execute(text(
                        f"ALTER TABLE {table.name} ADD COLUMN {col.name} {coltype}"
                    ))
                    conn.commit()
                    logger.warning(
                        "Added missing column %s.%s (%s) — the model declared it "
                        "but the database did not have it.",
                        table.name, col.name, coltype,
                    )
                except Exception:
                    conn.rollback()
