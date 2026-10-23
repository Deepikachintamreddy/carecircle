#!/usr/bin/env python3
"""Seed the CareCircle database with one demo family.

Creates:
  - Amma (patient), Ravi (caregiver), Priya (caregiver), Dr. Rao's Clinic (viewer)
  - 3 medications: BP tablet (2×/day), sugar tablet (1×/day), vitamin (1×/day)
  - 14 days of dose logs with a few realistic missed doses
  - 3 symptom notes (including one about dizziness)
  - 1 cardiology appointment next Monday
  - BP tablet at 3 days of stock

Prints each person's demo bearer token once to the console.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from server.db import (
    IST,
    execute,
    get_connection,
    init_db,
    now_ist,
    set_db_path,
)
from server.security import generate_token, hash_token


def next_weekday(start: datetime, weekday: int) -> datetime:
    """Return the next date >= *start* that falls on *weekday* (0=Mon)."""
    days_ahead = weekday - start.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return start + timedelta(days=days_ahead)


def seed() -> None:
    db_path = os.environ.get("DATABASE_PATH", "carecircle.db")
    set_db_path(db_path)

    # Wipe and recreate
    try:
        if Path(db_path).exists():
            Path(db_path).unlink()
    except Exception:
        conn = get_connection()
        for tbl in ("refill_orders", "dose_logs", "privacy_log", "notes", "appointments", "medications", "people"):
            try:
                conn.execute(f"DROP TABLE IF EXISTS {tbl}")
            except Exception:
                pass
        conn.commit()
        conn.close()

    init_db()

    # ── People ────────────────────────────────────────────────────────
    tokens: dict[str, str] = {}
    people_data = [
        ("Amma", "patient"),
        ("Ravi", "caregiver"),
        ("Priya", "caregiver"),
        ("Dr. Rao's Clinic", "viewer"),
    ]

    person_ids: dict[str, int] = {}
    for name, role in people_data:
        token = generate_token()
        tokens[name] = token
        pid = execute(
            "INSERT INTO people (name, role, token_hash) VALUES (?, ?, ?)",
            (name, role, hash_token(token)),
        )
        person_ids[name] = pid

    amma_id = person_ids["Amma"]
    ravi_id = person_ids["Ravi"]
    priya_id = person_ids["Priya"]

    # ── Medications ───────────────────────────────────────────────────
    bp_id = execute(
        "INSERT INTO medications (person_id, name, dose, times_json, aliases_json, pills_left, pills_per_day) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            amma_id,
            "Amlodipine (BP tablet)",
            "5 mg",
            json.dumps(["08:00", "20:00"]),
            json.dumps(["BP tablet", "blood pressure tablet", "BP medicine", "bp tab"]),
            6,   # 3 days × 2 per day
            2,
        ),
    )

    sugar_id = execute(
        "INSERT INTO medications (person_id, name, dose, times_json, aliases_json, pills_left, pills_per_day) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            amma_id,
            "Metformin (sugar tablet)",
            "500 mg",
            json.dumps(["14:00"]),
            json.dumps(["sugar tablet", "diabetes tablet", "sugar medicine", "sugar tab"]),
            30,
            1,
        ),
    )

    vitamin_id = execute(
        "INSERT INTO medications (person_id, name, dose, times_json, aliases_json, pills_left, pills_per_day) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            amma_id,
            "Vitamin D3",
            "1000 IU",
            json.dumps(["09:00"]),
            json.dumps(["vitamin", "vitamin D", "D3"]),
            60,
            1,
        ),
    )

    # ── Dose Logs (14 days) ───────────────────────────────────────────
    now = datetime.now(IST)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Days where doses are missed (relative to today, 0 = today)
    bp_morning_misses = {3, 7, 11, 0}   # missed morning BP (0 = today, so Amma logs it in Step 1)
    bp_evening_misses = {5}             # missed evening BP
    sugar_misses = {2, 7, 10, 0}        # missed sugar tablet (day 0 = today, 2 PM hasn't come yet for demo)
    vitamin_misses = {4, 9, 0}          # missed vitamin (day 0 = today pending)

    for days_ago in range(13, -1, -1):
        day = today - timedelta(days=days_ago)

        # BP morning (08:00-08:30)
        if days_ago not in bp_morning_misses:
            taken = day.replace(hour=8, minute=10 + (days_ago % 20))
            execute(
                "INSERT INTO dose_logs (medication_id, logged_by, taken_at) VALUES (?, ?, ?)",
                (bp_id, amma_id, taken.isoformat(timespec="seconds")),
            )

        # BP evening (20:00-20:30)
        if days_ago not in bp_evening_misses and days_ago != 0:
            # Skip today's evening dose (hasn't happened yet)
            taken = day.replace(hour=20, minute=5 + (days_ago % 25))
            execute(
                "INSERT INTO dose_logs (medication_id, logged_by, taken_at) VALUES (?, ?, ?)",
                (bp_id, amma_id, taken.isoformat(timespec="seconds")),
            )

        # Sugar tablet (14:00)
        if days_ago not in sugar_misses and days_ago != 0:
            taken = day.replace(hour=14, minute=5 + (days_ago % 15))
            execute(
                "INSERT INTO dose_logs (medication_id, logged_by, taken_at) VALUES (?, ?, ?)",
                (sugar_id, amma_id, taken.isoformat(timespec="seconds")),
            )

        # Vitamin (09:00)
        if days_ago not in vitamin_misses:
            taken = day.replace(hour=9, minute=0 + (days_ago % 10))
            execute(
                "INSERT INTO dose_logs (medication_id, logged_by, taken_at) VALUES (?, ?, ?)",
                (vitamin_id, amma_id, taken.isoformat(timespec="seconds")),
            )

    # ── Notes ─────────────────────────────────────────────────────────
    execute(
        "INSERT INTO notes (person_id, author_id, kind, text, created_at) VALUES (?, ?, ?, ?, ?)",
        (
            amma_id,
            ravi_id,
            "symptom",
            "Amma complained of dizziness after standing up quickly in the morning.",
            (today - timedelta(days=5)).replace(hour=9, minute=30).isoformat(timespec="seconds"),
        ),
    )
    execute(
        "INSERT INTO notes (person_id, author_id, kind, text, created_at) VALUES (?, ?, ?, ?, ?)",
        (
            amma_id,
            priya_id,
            "symptom",
            "Mild swelling in ankles noticed in the evening.",
            (today - timedelta(days=3)).replace(hour=18, minute=0).isoformat(timespec="seconds"),
        ),
    )
    execute(
        "INSERT INTO notes (person_id, author_id, kind, text, created_at) VALUES (?, ?, ?, ?, ?)",
        (
            amma_id,
            amma_id,
            "symptom",
            "Felt a slight headache after lunch, went away after resting.",
            (today - timedelta(days=1)).replace(hour=13, minute=45).isoformat(timespec="seconds"),
        ),
    )

    # ── Appointments ──────────────────────────────────────────────────
    next_monday = next_weekday(today, 0)  # 0 = Monday
    appt_time = next_monday.replace(hour=10, minute=30)
    execute(
        "INSERT INTO appointments (person_id, doctor, specialty, starts_at) VALUES (?, ?, ?, ?)",
        (amma_id, "Dr. Rao", "Cardiology", appt_time.isoformat(timespec="seconds")),
    )

    # ── Print tokens ──────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  CareCircle — Demo Tokens")
    print("=" * 60)
    for name, token in tokens.items():
        print(f"  {name:20s}  {token}")
    print("=" * 60)

    # Save tokens to .tokens.json for tests and clients
    (PROJECT_ROOT / "scripts" / ".tokens.json").write_text(json.dumps(tokens, indent=2))


    # Verify counts
    conn = get_connection()
    counts = {
        "people": conn.execute("SELECT COUNT(*) FROM people").fetchone()[0],
        "medications": conn.execute("SELECT COUNT(*) FROM medications").fetchone()[0],
        "dose_logs": conn.execute("SELECT COUNT(*) FROM dose_logs").fetchone()[0],
        "notes": conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0],
        "appointments": conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0],
    }
    conn.close()

    print("\nSeeded data:")
    for table, count in counts.items():
        print(f"  {table:20s}  {count}")
    print()


if __name__ == "__main__":
    seed()
