"""Why is Clinic Admin not showing for this doctor?

Prints every condition the gate checks, so the answer is a fact rather than a
guess. Read-only unless --fix is passed.

    python scripts/diagnose_clinic_admin.py doctor@example.com
    python scripts/diagnose_clinic_admin.py doctor@example.com --fix

--fix promotes the doctor's own clinic to the Clinic tier and aligns its
entitlement with their current trial/plan. Intended for accounts created
before "Clinic Account" at signup actually did anything, and for testing.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta
from database.connection import SessionLocal
from database.models import Doctor, Clinic, ClinicDoctor
from services.clinic_context import (
    active_memberships, owned_clinic, clinic_plan_active, ROLE_OWNER,
)

def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    email = sys.argv[1].strip().lower()
    do_fix = "--fix" in sys.argv
    now = datetime.utcnow()
    db = SessionLocal()
    try:
        d = db.query(Doctor).filter(Doctor.email == email).first()
        if not d:
            print(f"No doctor with email {email!r}")
            emails = [x.email for x in db.query(Doctor).all()]
            print("Known emails:", ", ".join(emails[:30]) or "(none)")
            sys.exit(1)

        print(f"Doctor #{d.id}  {d.name}  <{d.email}>")
        print(f"  active           : {d.is_active}")
        print(f"  email verified   : {bool(d.email_verified_at)}")
        print(f"  trial_ends_at    : {d.trial_ends_at}   (live: {bool(d.trial_ends_at and d.trial_ends_at > now)})")
        print(f"  plan_expires_at  : {d.plan_expires_at} (live: {bool(d.plan_expires_at and d.plan_expires_at > now)})")

        ms = active_memberships(db, d.id)
        print(f"\n  memberships ({len(ms)}):")
        for m in ms:
            c = db.query(Clinic).filter(Clinic.id == m.clinic_id).first()
            print(f"    clinic {m.clinic_id} role={m.role:9} name={c.name if c else '?'!r}")
            if c:
                print(f"       plan_type        = {c.plan_type!r}      <-- must be 'clinic'")
                print(f"       plan_expires_at  = {c.plan_expires_at}")
                print(f"       plan_grace_until = {c.plan_grace_until}")
                print(f"       max_doctors      = {c.max_doctors}")
                print(f"       entitlement live = {clinic_plan_active(c, db, now)}")
        if not ms:
            print("    (none — this doctor belongs to no clinic at all)")

        owned = owned_clinic(db, d.id)
        paid  = owned_clinic(db, d.id, require_paid=True)
        print(f"\n  owns a clinic          : {bool(owned)}  {owned.name if owned else ''}")
        print(f"  CLINIC ADMIN VISIBLE   : {bool(paid)}")

        if not paid:
            print("\n  Why not:")
            if not owned:
                print("    - This doctor does not OWN any clinic (associate only).")
            else:
                if owned.plan_type != "clinic":
                    print(f"    - Their clinic is plan_type={owned.plan_type!r}, not 'clinic'.")
                    print("      Accounts created before 'Clinic Account' at signup took effect")
                    print("      stayed on 'trial'. Buying a Clinic/Duo/Hospital plan sets it,")
                    print("      or re-run this with --fix.")
                elif not clinic_plan_active(owned, db, now):
                    print("    - Clinic is tier 'clinic' but its entitlement has lapsed:")
                    print(f"      plan_expires_at={owned.plan_expires_at}, grace={owned.plan_grace_until},")
                    print(f"      owner trial={d.trial_ends_at}, owner plan={d.plan_expires_at}")

        if do_fix and owned:
            end = max([x for x in (d.trial_ends_at, d.plan_expires_at, now + timedelta(days=14)) if x])
            owned.plan_type = "clinic"
            owned.plan_expires_at = end
            owned.plan_grace_until = end + timedelta(days=7)
            if (owned.max_doctors or 1) < 5:
                owned.max_doctors = 5
            db.commit()
            print(f"\n  FIXED: clinic {owned.id!r} -> tier 'clinic', entitlement until {end}, 5 seats.")
            print(f"  Clinic Admin visible now: {bool(owned_clinic(db, d.id, require_paid=True))}")
        elif do_fix:
            print("\n  --fix skipped: this doctor owns no clinic to promote.")
    finally:
        db.close()

if __name__ == "__main__":
    main()
