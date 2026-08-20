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
        _add_column(conn, "ALTER TABLE clinics ADD COLUMN plan_grace_until DATETIME")
        _add_column(conn, "ALTER TABLE clinics ADD COLUMN max_doctors INTEGER DEFAULT 1")
        _add_column(conn, "ALTER TABLE clinics ADD COLUMN max_staff INTEGER DEFAULT 2")
        _add_column(conn, "ALTER TABLE clinics ADD COLUMN billing_access_staff BOOLEAN DEFAULT FALSE")
        # Backfill: solo-plan clinics keep max_doctors=1; set Clinic plan ones to 5
        conn.execute(text(
            "UPDATE clinics SET max_doctors = 5, max_staff = 999 "
            "WHERE plan_type = 'clinic'"
        ))
        conn.commit()

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

        # ── Performance: composite indexes on the hottest filter columns ─────
        # (doctor_id + date) covers the appointment-list and queue queries.
        # CREATE INDEX IF NOT EXISTS works on both SQLite and PostgreSQL.
        for _ix_sql in (
            "CREATE INDEX IF NOT EXISTS ix_appointments_doctor_date "
            "ON appointments (doctor_id, appointment_date)",
            "CREATE INDEX IF NOT EXISTS ix_visits_doctor_date "
            "ON visits (doctor_id, visit_date)",
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
