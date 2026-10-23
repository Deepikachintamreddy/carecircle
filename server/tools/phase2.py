"""CareCircle Phase 2 Tools.

Implements the first 6 tools with role checks, natural spoken replies,
and structured data:
  1. log_dose
  2. whats_due
  3. add_medication
  4. add_note
  5. add_appointment
  6. care_summary
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from mcp.server.mcpserver import Context

from server.db import (
    IST,
    execute,
    fetch_all,
    fetch_one,
    find_medication,
    get_patient_id,
    now_ist,
)
from server.security import get_caller, require_role


def _format_time_display(dt_str: str) -> str:
    """Format an ISO timestamp or HH:MM string into a friendly time like '8:10 AM'."""
    try:
        if "T" in dt_str:
            dt = datetime.fromisoformat(dt_str)
            return dt.strftime("%I:%M %p").lstrip("0")
        elif ":" in dt_str:
            parts = dt_str.split(":")
            h = int(parts[0])
            m = int(parts[1])
            period = "AM" if h < 12 else "PM"
            display_h = h % 12
            if display_h == 0:
                display_h = 12
            return f"{display_h}:{m:02d} {period}"
    except Exception:
        pass
    return dt_str


def _parse_time_to_iso(time_str: str | None) -> str:
    """Parse time input into an ISO timestamp with Asia/Kolkata timezone."""
    now = datetime.now(IST)
    if not time_str or not time_str.strip():
        return now.isoformat(timespec="seconds")

    s = time_str.strip()
    # Check if already ISO format
    if "T" in s:
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=IST)
            return dt.isoformat(timespec="seconds")
        except Exception:
            pass

    # Parse 12-hour or 24-hour time like '8:10', '08:10', '8:10 AM', '14:00'
    for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M", "%H:%M:%S"):
        try:
            parsed = datetime.strptime(s.upper(), fmt)
            combined = now.replace(
                hour=parsed.hour,
                minute=parsed.minute,
                second=parsed.second if fmt == "%H:%M:%S" else 0,
                microsecond=0,
            )
            return combined.isoformat(timespec="seconds")
        except ValueError:
            continue

    return now.isoformat(timespec="seconds")


# ── Tool 1: log_dose ──────────────────────────────────────────────────

async def log_dose(
    medicine: str,
    time: str | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Log that the patient has taken a medication dose.

    Args:
        medicine: Name or alias of the medication (e.g. 'BP tablet', 'sugar tablet').
        time: Optional time taken (e.g. '08:10', '8:10 AM'). Defaults to now.
    """
    caller = get_caller(ctx)
    err = require_role(caller, "patient", "caregiver")
    if err:
        return {"spoken_response": "Only patients and caregivers can log doses.", "data": {"error": err}}

    patient_id = get_patient_id()
    patient = fetch_one("SELECT name FROM people WHERE id = ?", (patient_id,))
    patient_name = patient["name"] if patient else "the patient"

    med = find_medication(medicine, patient_id)
    if not med:
        return {
            "spoken_response": f"I couldn't find a medication matching '{medicine}'.",
            "data": {"error": "medication_not_found", "query": medicine},
        }

    taken_at = _parse_time_to_iso(time)
    time_display = _format_time_display(taken_at)

    log_id = execute(
        "INSERT INTO dose_logs (medication_id, logged_by, taken_at) VALUES (?, ?, ?)",
        (med["id"], caller["id"], taken_at),
    )

    new_pills = max(0, med["pills_left"] - 1)
    execute("UPDATE medications SET pills_left = ? WHERE id = ?", (new_pills, med["id"]))

    spoken = (
        f"I've logged your {med['name']} dose at {time_display}."
        if caller["id"] == patient_id
        else f"Logged {med['name']} for {patient_name} at {time_display}."
    )

    return {
        "spoken_response": spoken,
        "data": {
            "id": log_id,
            "medication_id": med["id"],
            "medication_name": med["name"],
            "dose": med["dose"],
            "taken_at": taken_at,
            "logged_by": caller["name"],
            "pills_left": new_pills,
        },
    }


# ── Tool 2: whats_due ─────────────────────────────────────────────────

async def whats_due(
    date: str | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Check taken, pending, and missed medication doses for a given date.

    Args:
        date: Date in YYYY-MM-DD format. Defaults to today.
    """
    caller = get_caller(ctx)
    patient_id = get_patient_id()
    patient = fetch_one("SELECT name FROM people WHERE id = ?", (patient_id,))
    patient_name = patient["name"] if patient else "the patient"

    now = datetime.now(IST)
    today_str = now.strftime("%Y-%m-%d")
    target_date = date.strip() if date and date.strip() else today_str

    meds = fetch_all("SELECT * FROM medications WHERE person_id = ?", (patient_id,))
    logs = fetch_all(
        "SELECT dl.*, m.name as med_name FROM dose_logs dl "
        "JOIN medications m ON dl.medication_id = m.id "
        "WHERE dl.taken_at LIKE ? ORDER BY dl.taken_at ASC",
        (f"{target_date}%",),
    )

    taken: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    missed: list[dict[str, Any]] = []

    now_hm = now.strftime("%H:%M")
    is_today = (target_date == today_str)

    for med in meds:
        times: list[str] = json.loads(med.get("times_json", "[]"))
        med_logs = [l for l in logs if l["medication_id"] == med["id"]]

        for idx, slot_time in enumerate(times):
            slot_display = _format_time_display(slot_time)
            if idx < len(med_logs):
                log_entry = med_logs[idx]
                taken_display = _format_time_display(log_entry["taken_at"])
                taken.append({
                    "medication": med["name"],
                    "scheduled_time": slot_time,
                    "taken_at": log_entry["taken_at"],
                    "display": f"{med['name']} at {taken_display}",
                })
            else:
                item = {
                    "medication": med["name"],
                    "scheduled_time": slot_time,
                    "display": f"{slot_display} {med['name']}",
                }
                if not is_today and target_date < today_str:
                    missed.append(item)
                elif is_today and slot_time < now_hm:
                    missed.append(item)
                else:
                    pending.append(item)

    # Formulate concise spoken summary (max 2 sentences)
    sentences = []
    if taken:
        taken_parts = [f"{t['medication']} at {_format_time_display(t['taken_at'])}" for t in taken]
        sentences.append(f"She took the {', and '.join(taken_parts)}.")
    else:
        sentences.append(f"No medicines have been logged yet for {patient_name} today.")

    if missed:
        missed_names = [f"{_format_time_display(m['scheduled_time'])} {m['medication']}" for m in missed]
        sentences.append(f"The {', and '.join(missed_names)} is not logged yet.")
    elif pending:
        pending_names = [f"{_format_time_display(p['scheduled_time'])} {p['medication']}" for p in pending]
        sentences.append(f"Upcoming doses are {', and '.join(pending_names)}.")
    elif taken and not missed and not pending:
        sentences.append("All scheduled medicines are complete for the day.")

    spoken = " ".join(sentences[:2])

    return {
        "spoken_response": spoken,
        "data": {
            "date": target_date,
            "patient": patient_name,
            "taken": taken,
            "pending": pending,
            "missed": missed,
            "total_scheduled": len(taken) + len(pending) + len(missed),
        },
    }


# ── Tool 3: add_medication ────────────────────────────────────────────

async def add_medication(
    name: str,
    dose: str,
    times: str | list[str],
    pills_left: int = 0,
    pills_per_day: float = 1.0,
    aliases: list[str] | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Add a new medication to the patient's schedule.

    Args:
        name: Name of the medicine (e.g. 'Amlodipine').
        dose: Dosage (e.g. '5 mg').
        times: Daily times (e.g. ['08:00', '20:00'] or '08:00, 20:00').
        pills_left: Current number of pills in supply.
        pills_per_day: How many pills consumed per day.
        aliases: Optional common nicknames (e.g. ['BP tablet']).
    """
    caller = get_caller(ctx)
    err = require_role(caller, "patient", "caregiver")
    if err:
        return {"spoken_response": "Only caregivers and patients can add medications.", "data": {"error": err}}

    patient_id = get_patient_id()

    # Normalize times
    if isinstance(times, str):
        parsed_times = [t.strip() for t in times.split(",") if t.strip()]
    else:
        parsed_times = list(times)

    alias_list = list(aliases) if aliases else []
    if name not in alias_list:
        alias_list.append(name)

    med_id = execute(
        "INSERT INTO medications (person_id, name, dose, times_json, aliases_json, pills_left, pills_per_day) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            patient_id,
            name.strip(),
            dose.strip(),
            json.dumps(parsed_times),
            json.dumps(alias_list),
            int(pills_left),
            float(pills_per_day),
        ),
    )

    spoken = f"Added {name} ({dose}) with {pills_left} pills in stock."
    return {
        "spoken_response": spoken,
        "data": {
            "id": med_id,
            "name": name,
            "dose": dose,
            "times": parsed_times,
            "pills_left": pills_left,
            "pills_per_day": pills_per_day,
            "aliases": alias_list,
        },
    }


# ── Tool 4: add_note ──────────────────────────────────────────────────

async def add_note(
    text: str,
    kind: str = "general",
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Record a symptom or general observation note for the family.

    Args:
        text: The content of the note (e.g. 'Felt dizzy after standing up').
        kind: 'symptom' or 'general'. Auto-detected if symptom words present.
    """
    caller = get_caller(ctx)
    err = require_role(caller, "patient", "caregiver")
    if err:
        return {"spoken_response": "Only caregivers and patients can add notes.", "data": {"error": err}}

    patient_id = get_patient_id()

    # Auto-detect symptom keywords
    symptom_keywords = ("dizzy", "dizziness", "headache", "pain", "nausea", "fever", "fall", "pressure", "tired", "fatigue")
    final_kind = kind.lower().strip()
    if final_kind not in ("symptom", "general"):
        final_kind = "symptom" if any(k in text.lower() for k in symptom_keywords) else "general"

    created_at = now_ist()
    note_id = execute(
        "INSERT INTO notes (person_id, author_id, kind, text, created_at) VALUES (?, ?, ?, ?, ?)",
        (patient_id, caller["id"], final_kind, text.strip(), created_at),
    )

    spoken = f"Added a {final_kind} note: '{text.strip()}'."
    return {
        "spoken_response": spoken,
        "data": {
            "id": note_id,
            "kind": final_kind,
            "text": text.strip(),
            "author": caller["name"],
            "created_at": created_at,
        },
    }


# ── Tool 5: add_appointment ───────────────────────────────────────────

async def add_appointment(
    doctor: str,
    specialty: str,
    starts_at: str,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Schedule a medical appointment for the patient.

    Args:
        doctor: Doctor's name (e.g. 'Dr. Rao').
        specialty: Medical specialty (e.g. 'Cardiology').
        starts_at: Date and time of appointment in ISO format or friendly string.
    """
    caller = get_caller(ctx)
    err = require_role(caller, "patient", "caregiver")
    if err:
        return {"spoken_response": "Only caregivers and patients can add appointments.", "data": {"error": err}}

    patient_id = get_patient_id()

    appt_id = execute(
        "INSERT INTO appointments (person_id, doctor, specialty, starts_at) VALUES (?, ?, ?, ?)",
        (patient_id, doctor.strip(), specialty.strip(), starts_at.strip()),
    )

    spoken = f"Scheduled appointment with Dr. {doctor} ({specialty}) for {starts_at}."
    return {
        "spoken_response": spoken,
        "data": {
            "id": appt_id,
            "doctor": doctor,
            "specialty": specialty,
            "starts_at": starts_at,
        },
    }


# ── Tool 6: care_summary ──────────────────────────────────────────────

async def care_summary(
    days: int = 14,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Generate a high-level care summary for the family or clinic over the past N days.

    Args:
        days: Number of days to summarize (default: 14).
    """
    caller = get_caller(ctx)  # All roles (patient, caregiver, viewer) permitted
    patient_id = get_patient_id()
    patient = fetch_one("SELECT name FROM people WHERE id = ?", (patient_id,))
    patient_name = patient["name"] if patient else "the patient"

    now = datetime.now(IST)
    start_dt = now - timedelta(days=days)
    start_str = start_dt.strftime("%Y-%m-%d")

    # Dose adherence
    meds = fetch_all("SELECT * FROM medications WHERE person_id = ?", (patient_id,))
    logs = fetch_all(
        "SELECT dl.*, m.name as med_name FROM dose_logs dl "
        "JOIN medications m ON dl.medication_id = m.id "
        "WHERE dl.taken_at >= ? ORDER BY dl.taken_at ASC",
        (start_str,),
    )

    # Estimate expected doses
    total_expected = sum(int(len(json.loads(m.get("times_json", "[]"))) * days) for m in meds)
    total_taken = len(logs)
    missed_count = max(0, total_expected - total_taken)
    adherence_pct = round((total_taken / max(1, total_expected)) * 100, 1)

    # Symptoms in window
    notes = fetch_all(
        "SELECT n.*, p.name as author_name FROM notes n "
        "JOIN people p ON n.author_id = p.id "
        "WHERE n.person_id = ? AND n.kind = 'symptom' AND n.created_at >= ? "
        "ORDER BY n.created_at DESC",
        (patient_id, start_str),
    )

    # Supply alerts (<= 5 days left)
    supply_alerts = []
    for m in meds:
        daily = m.get("pills_per_day", 1.0)
        days_left = round(m["pills_left"] / max(0.1, daily), 1)
        if days_left <= 5:
            supply_alerts.append({
                "medication": m["name"],
                "pills_left": m["pills_left"],
                "days_left": days_left,
            })

    # Upcoming appointments
    appointments = fetch_all(
        "SELECT * FROM appointments WHERE person_id = ? AND starts_at >= ? ORDER BY starts_at ASC",
        (patient_id, now.strftime("%Y-%m-%d")),
    )

    # Spoken summary
    parts = [f"Over the last {days} days, {patient_name} took {total_taken} of {total_expected} scheduled doses ({adherence_pct}% adherence)."]
    if supply_alerts:
        first_alert = supply_alerts[0]
        parts.append(f"{first_alert['medication']} is low with {int(first_alert['days_left'])} days left.")
    elif appointments:
        first_appt = appointments[0]
        parts.append(f"Upcoming appointment with {first_appt['doctor']} on {first_appt['starts_at'][:10]}.")

    spoken = " ".join(parts[:2])

    return {
        "spoken_response": spoken,
        "data": {
            "days": days,
            "adherence_percentage": adherence_pct,
            "doses_taken": total_taken,
            "doses_expected": total_expected,
            "doses_missed": missed_count,
            "recent_symptoms": [
                {"id": n["id"], "text": n["text"], "date": n["created_at"][:10], "author": n["author_name"]}
                for n in notes
            ],
            "supply_alerts": supply_alerts,
            "upcoming_appointments": [
                {"id": a["id"], "doctor": a["doctor"], "specialty": a["specialty"], "starts_at": a["starts_at"]}
                for a in appointments
            ],
        },
    }
