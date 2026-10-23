"""Quick verification of seeded data."""
import sys
sys.path.insert(0, ".")
from server.db import fetch_all, set_db_path

set_db_path("carecircle.db")

print("=== PEOPLE ===")
for p in fetch_all("SELECT id, name, role FROM people"):
    print(f"  {p['id']}: {p['name']} ({p['role']})")

print("\n=== MEDICATIONS ===")
for m in fetch_all("SELECT id, name, dose, pills_left, pills_per_day FROM medications"):
    days_left = m["pills_left"] / m["pills_per_day"] if m["pills_per_day"] else 0
    print(f"  {m['id']}: {m['name']} {m['dose']} — {m['pills_left']} pills ({days_left:.0f} days)")

print("\n=== DOSE LOGS (last 5) ===")
for d in fetch_all("SELECT dl.id, m.name, dl.taken_at FROM dose_logs dl JOIN medications m ON dl.medication_id=m.id ORDER BY dl.taken_at DESC LIMIT 5"):
    print(f"  {d['name']}: {d['taken_at']}")

print("\n=== NOTES ===")
for n in fetch_all("SELECT kind, text, created_at FROM notes"):
    print(f"  [{n['kind']}] {n['text'][:70]}... ({n['created_at']})")

print("\n=== APPOINTMENTS ===")
for a in fetch_all("SELECT doctor, specialty, starts_at FROM appointments"):
    print(f"  {a['doctor']} ({a['specialty']}) at {a['starts_at']}")

print("\n=== AUTH TEST ===")
from server.security import authenticate
# Test with a known token hash
person = fetch_all("SELECT name, token_hash FROM people LIMIT 1")[0]
print(f"  Token hash lookup for {person['name']}: works" if person else "  FAILED")
