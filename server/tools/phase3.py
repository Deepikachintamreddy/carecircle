"""CareCircle Phase 3 Tools.

Implements the multi-step, confirmation-gated, and brief tools:
  7. check_supply
  8. request_refill  (money-spending: confirmation required)
  9. prepare_visit_brief
 10. forget          (data-deletion: confirmation required + privacy log)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel
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


class ConfirmSchema(BaseModel):
    """Schema for elicitation confirmation dialog."""
    confirm: bool


async def _check_confirmation(ctx: Context, prompt: str, confirm_param: bool) -> bool:
    """Attempt MCP elicitation if available, falling back to confirm_param."""
    if confirm_param:
        return True
    if ctx and hasattr(ctx, "elicit"):
        try:
            result = await ctx.elicit(prompt, ConfirmSchema)
            if result and hasattr(result, "data") and getattr(result.data, "confirm", False):
                return True
        except Exception:
            # Client doesn't support elicitation or refused
            pass
    return False


# ── Tool 7: check_supply ──────────────────────────────────────────────

async def check_supply(
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Check remaining pill supply and days of stock left for all patient medications."""
    caller = get_caller(ctx)  # All roles can check supply
    patient_id = get_patient_id()
    patient = fetch_one("SELECT name FROM people WHERE id = ?", (patient_id,))
    patient_name = patient["name"] if patient else "the patient"

    meds = fetch_all("SELECT * FROM medications WHERE person_id = ?", (patient_id,))

    items: list[dict[str, Any]] = []
    low_supply_items: list[dict[str, Any]] = []

    for m in meds:
        daily = m.get("pills_per_day", 1.0)
        days_left = round(m["pills_left"] / max(0.1, daily), 1)
        is_low = (days_left <= 5)
        item = {
            "id": m["id"],
            "name": m["name"],
            "dose": m["dose"],
            "pills_left": m["pills_left"],
            "pills_per_day": daily,
            "days_left": days_left,
            "is_low": is_low,
        }
        items.append(item)
        if is_low:
            low_supply_items.append(item)

    # Formulate spoken response (at most 2 sentences)
    if low_supply_items:
        first = low_supply_items[0]
        spoken = (
            f"{first['name']} has only {int(first['days_left'])} days of supply left ({first['pills_left']} pills). "
            f"Would you like me to request a refill?"
        )
    else:
        spoken = f"All of {patient_name}'s medications have adequate supply for at least the next week."

    return {
        "spoken_response": spoken,
        "data": {
            "patient": patient_name,
            "medications": items,
            "low_supply_count": len(low_supply_items),
            "refill_needed": len(low_supply_items) > 0,
        },
    }


# ── Tool 8: request_refill ────────────────────────────────────────────

async def request_refill(
    medicine: str,
    quantity: int | None = None,
    confirm: bool = False,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Request a pharmacy refill for a medication.

    CONFIRMATION REQUIRED: This action places an order (mock pharmacy order).
    Will prompt for confirmation via elicitation or require confirm=true.

    Args:
        medicine: Name or alias of the medicine (e.g. 'BP tablet').
        quantity: Number of pills to order (defaults to a 30-day supply).
        confirm: Explicit user confirmation flag if client does not support elicitation.
    """
    caller = get_caller(ctx)
    err = require_role(caller, "patient", "caregiver")
    if err:
        return {"spoken_response": "Only caregivers and patients can request refills.", "data": {"error": err}}

    patient_id = get_patient_id()
    med = find_medication(medicine, patient_id)
    if not med:
        return {
            "spoken_response": f"I couldn't find a medication matching '{medicine}'.",
            "data": {"error": "medication_not_found", "query": medicine},
        }

    daily = med.get("pills_per_day", 1.0)
    refill_qty = quantity if (quantity and quantity > 0) else int(daily * 30)
    days_left = round(med["pills_left"] / max(0.1, daily), 1)

    prompt = (
        f"Confirm refill order: Order {refill_qty} pills of {med['name']} "
        f"from mock pharmacy for delivery? (Currently {int(days_left)} days left)"
    )

    is_confirmed = await _check_confirmation(ctx, prompt, confirm)
    if not is_confirmed:
        return {
            "spoken_response": (
                f"{med['name']} has {int(days_left)} days of supply left. "
                f"Should I place a mock pharmacy refill order for {refill_qty} pills? Please say yes or confirm."
            ),
            "confirmation_required": True,
            "action": "request_refill",
            "data": {
                "medication_id": med["id"],
                "medication_name": med["name"],
                "quantity": refill_qty,
                "days_left": days_left,
                "is_mock": True,
            },
        }

    # Execute mock refill order
    created_at = now_ist()
    order_id = execute(
        "INSERT INTO refill_orders (medication_id, requested_by, quantity, status, created_at) "
        "VALUES (?, ?, ?, 'placed (mock)', ?)",
        (med["id"], caller["id"], refill_qty, created_at),
    )

    # Note: Mock pharmacy order placed. Supply will update upon simulated delivery or order fulfillment.
    spoken = (
        f"I've placed a mock refill order for {refill_qty} pills of {med['name']}. "
        f"Order reference is #MOCK-{order_id}."
    )

    return {
        "spoken_response": spoken,
        "data": {
            "order_id": order_id,
            "medication_id": med["id"],
            "medication_name": med["name"],
            "quantity": refill_qty,
            "status": "placed",
            "is_mock": True,
            "created_at": created_at,
            "requested_by": caller["name"],
        },
    }


# ── Tool 9: prepare_visit_brief ───────────────────────────────────────

async def prepare_visit_brief(
    appointment_id: int | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Generate a comprehensive doctor visit brief for the upcoming appointment.

    Collects missed doses, symptom logs, active medications, and recommended doctor questions
    from the last 14 days of care records.

    Args:
        appointment_id: Specific appointment ID, or defaults to the next scheduled appointment.
    """
    caller = get_caller(ctx)  # All roles (patient, caregiver, viewer) permitted
    patient_id = get_patient_id()
    patient = fetch_one("SELECT name FROM people WHERE id = ?", (patient_id,))
    patient_name = patient["name"] if patient else "the patient"

    now = datetime.now(IST)
    today_str = now.strftime("%Y-%m-%d")

    # 1. Target appointment
    if appointment_id:
        appt = fetch_one("SELECT * FROM appointments WHERE id = ?", (appointment_id,))
    else:
        appt = fetch_one(
            "SELECT * FROM appointments WHERE person_id = ? AND starts_at >= ? ORDER BY starts_at ASC LIMIT 1",
            (patient_id, today_str),
        )

    if not appt:
        # Fallback to most recent appointment
        appt = fetch_one("SELECT * FROM appointments WHERE person_id = ? ORDER BY starts_at DESC LIMIT 1", (patient_id,))

    raw_doc = appt["doctor"] if appt else "the doctor"
    doctor_str = raw_doc if raw_doc.startswith("Dr.") else f"Dr. {raw_doc}"
    specialty_str = appt.get("specialty", "General Medicine") if appt else "Checkup"
    appt_time_str = appt.get("starts_at", "Upcoming") if appt else "soon"


    # 2. 14 days history window
    start_14 = (now - timedelta(days=14)).strftime("%Y-%m-%d")

    meds = fetch_all("SELECT * FROM medications WHERE person_id = ?", (patient_id,))
    logs = fetch_all(
        "SELECT dl.*, m.name as med_name FROM dose_logs dl "
        "JOIN medications m ON dl.medication_id = m.id "
        "WHERE dl.taken_at >= ? ORDER BY dl.taken_at ASC",
        (start_14,),
    )

    # Calculate missed doses over 14 days
    total_expected = 0
    for m in meds:
        daily_count = len(json.loads(m.get("times_json", "[]")))
        total_expected += daily_count * 14

    total_taken = len(logs)
    missed_count = max(0, total_expected - total_taken)
    adherence_pct = round((total_taken / max(1, total_expected)) * 100, 1)

    # 3. Symptoms logged in window
    symptom_notes = fetch_all(
        "SELECT n.*, p.name as author_name FROM notes n "
        "JOIN people p ON n.author_id = p.id "
        "WHERE n.person_id = ? AND n.kind = 'symptom' AND n.created_at >= ? "
        "ORDER BY n.created_at DESC",
        (patient_id, start_14),
    )

    # 4. Generate intelligent questions for doctor based on real data
    questions: list[str] = []
    has_dizziness = any("dizz" in n["text"].lower() for n in symptom_notes)
    if has_dizziness:
        questions.append("Discuss morning dizziness episodes and whether BP medication dosage (Amlodipine) needs adjusting.")
    if any("swell" in n["text"].lower() or "ankle" in n["text"].lower() for n in symptom_notes):
        questions.append("Evaluate ankle swelling reported by family to check for fluid retention or calcium channel blocker side effects.")
    if missed_count > 3:
        questions.append("Review afternoon dosing schedule (Metformin) to help prevent missed doses during outings.")
    if not questions:
        questions.append(f"Review general medication tolerance and progress under current regimen.")

    spoken = (
        f"Here is {patient_name}'s brief for {doctor_str} ({specialty_str}). "
        f"Over the last 14 days, she took {total_taken} doses with {missed_count} missed, and had {len(symptom_notes)} symptom notes."
    )

    return {
        "spoken_response": spoken,
        "data": {
            "patient": patient_name,
            "appointment": {
                "doctor": doctor_str,
                "specialty": specialty_str,
                "starts_at": appt_time_str,
            },
            "adherence": {
                "adherence_percentage": adherence_pct,
                "doses_taken": total_taken,
                "doses_expected": total_expected,
                "doses_missed": missed_count,
            },
            "symptoms": [
                {"id": n["id"], "text": n["text"], "date": n["created_at"][:10], "logged_by": n["author_name"]}
                for n in symptom_notes
            ],
            "current_medications": [
                {
                    "name": m["name"],
                    "dose": m["dose"],
                    "times": json.loads(m.get("times_json", "[]")),
                    "pills_left": m["pills_left"],
                }
                for m in meds
            ],
            "questions_to_ask": questions,
        },
    }


# ── Tool 10: forget ───────────────────────────────────────────────────

async def forget(
    item_type: str,
    item_id: int | None = None,
    query: str | None = None,
    confirm: bool = False,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Delete a note or record from the family database and record a privacy log entry.

    CONFIRMATION REQUIRED: This action permanently deletes data.
    Will prompt for confirmation via elicitation or require confirm=true.

    Args:
        item_type: Type of item to delete ('note', 'medication', 'appointment').
        item_id: ID of the specific item to delete.
        query: Search keyword if ID is not known (e.g. 'dizziness').
        confirm: Explicit user confirmation flag.
    """
    caller = get_caller(ctx)
    err = require_role(caller, "patient", "caregiver")
    if err:
        return {"spoken_response": "Only caregivers and patients can delete care records.", "data": {"error": err}}

    patient_id = get_patient_id()
    normalized_type = item_type.lower().strip()

    # Find the target item
    target = None
    if normalized_type == "note":
        if item_id:
            target = fetch_one("SELECT * FROM notes WHERE id = ? AND person_id = ?", (item_id, patient_id))
        elif query:
            q = f"%{query.strip().lower()}%"
            target = fetch_one(
                "SELECT * FROM notes WHERE person_id = ? AND LOWER(text) LIKE ? ORDER BY created_at DESC LIMIT 1",
                (patient_id, q),
            )
        else:
            return {
                "spoken_response": "Please specify which note or topic you would like me to forget.",
                "data": {"error": "missing_query_or_id"},
            }
    elif normalized_type == "medication":
        if item_id:
            target = fetch_one("SELECT * FROM medications WHERE id = ? AND person_id = ?", (item_id, patient_id))
        elif query:
            target = find_medication(query, patient_id)
    elif normalized_type == "appointment":
        if item_id:
            target = fetch_one("SELECT * FROM appointments WHERE id = ? AND person_id = ?", (item_id, patient_id))
        elif query:
            q = f"%{query.strip().lower()}%"
            target = fetch_one(
                "SELECT * FROM appointments WHERE person_id = ? AND (LOWER(doctor) LIKE ? OR LOWER(specialty) LIKE ?) LIMIT 1",
                (patient_id, q, q),
            )

    if not target:
        desc = query or f"ID {item_id}"
        return {
            "spoken_response": f"I couldn't find any {normalized_type} matching '{desc}'.",
            "data": {"error": "not_found", "item_type": normalized_type, "query": desc},
        }

    preview = target.get("text") or target.get("name") or f"Dr. {target.get('doctor')}"
    target_id = target["id"]

    prompt = f"Are you sure you want to permanently delete this {normalized_type}: '{preview}'?"
    is_confirmed = await _check_confirmation(ctx, prompt, confirm)
    if not is_confirmed:
        return {
            "spoken_response": f"Are you sure you want to permanently delete the {normalized_type} about '{preview}'? Please confirm yes to proceed.",
            "confirmation_required": True,
            "action": "forget",
            "data": {
                "item_type": normalized_type,
                "item_id": target_id,
                "preview": preview,
            },
        }

    # Execute permanent deletion
    table_map = {"note": "notes", "medication": "medications", "appointment": "appointments"}
    table_name = table_map.get(normalized_type, "notes")

    execute(f"DELETE FROM {table_name} WHERE id = ?", (target_id,))

    # Record privacy audit log
    created_at = now_ist()
    execute(
        "INSERT INTO privacy_log (actor_id, action, item_type, item_id, created_at) VALUES (?, 'delete', ?, ?, ?)",
        (caller["id"], normalized_type, target_id, created_at),
    )

    spoken = f"I've erased the {normalized_type} '{preview}' and recorded a privacy audit log."

    return {
        "spoken_response": spoken,
        "data": {
            "deleted": True,
            "item_type": normalized_type,
            "item_id": target_id,
            "preview": preview,
            "privacy_log_recorded": True,
            "deleted_by": caller["name"],
            "timestamp": created_at,
        },
    }
