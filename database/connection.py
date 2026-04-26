from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from config import settings

engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in settings.DATABASE_URL else {},
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
        """Run ALTER TABLE … ADD COLUMN, silently ignore if column exists."""
        try:
            conn.execute(text(sql))
            conn.commit()
        except Exception:
            pass

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
        doctors_without_clinic = conn.execute(text(
            "SELECT d.id, d.clinic_name, d.clinic_address, d.city, d.slug "
            "FROM doctors d "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM clinic_doctors cd WHERE cd.doctor_id = d.id"
            ")"
        )).fetchall()

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

        # ── Phase 3: Patient notes & file attachments ────────────────────────
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS patient_notes ("
            "  id         INTEGER PRIMARY KEY AUTOINCREMENT, "
            "  patient_id INTEGER NOT NULL REFERENCES patients(id), "
            "  doctor_id  INTEGER NOT NULL REFERENCES doctors(id), "
            "  note_text  TEXT    NOT NULL, "
            "  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            ")"
        ))
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS note_files ("
            "  id            INTEGER PRIMARY KEY AUTOINCREMENT, "
            "  note_id       INTEGER NOT NULL REFERENCES patient_notes(id), "
            "  original_name VARCHAR(255) NOT NULL, "
            "  stored_name   VARCHAR(255) NOT NULL, "
            "  file_size     INTEGER, "
            "  uploaded_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            ")"
        ))
        conn.commit()
