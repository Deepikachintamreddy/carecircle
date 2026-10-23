---
name: carecircle
description: Alexa+ shared family care coordinator add-on for aging parents, managing medication schedules, dose tracking, supply refills, symptom notes, and doctor visit briefs.
---

# CareCircle Agent Skill Guide

This skill guides an AI assistant (such as Alexa+) on how to correctly and safely interact with the CareCircle MCP Server.

## Core Principles
1. **Concise Spoken Responses**: The user is listening via a smart speaker or voice assistant. Always keep spoken replies to **at most 2 sentences**. Never read out large tables or JSON over voice.
2. **Explicit Confirmation Before High-Impact Actions**:
   - **Spending Money**: Placing pharmacy orders (`request_refill`) must ALWAYS ask for explicit confirmation before executing. State the quantity and estimated supply remaining.
   - **Deleting Data**: Erasing records (`forget`) must ALWAYS confirm what note or item is being erased before proceeding.
3. **Role Awareness**: The server determines the speaker from the authenticated bearer token. Never ask the user for their role or token. Respect permissions:
   - `patient` and `caregiver`: Read and write permissions.
   - `viewer` (e.g. Dr. Rao's Clinic): Read-only access to summaries and visit briefs. Attempting write operations will return a friendly rejection.
4. **Fuzzy Medication Matching**: Amma and family members use informal names ("BP tablet", "sugar tablet", "my vitamin"). The system automatically fuzzy matches these to canonical pharmaceutical records.

---

## Tool Reference

### 1. `log_dose(medicine: str, time: str | None = None)`
- **When to use**: When the patient or caregiver states that medicine was taken (e.g., "I took my BP tablet", "Amma took her morning medicine at 8:15").
- **Parameters**:
  - `medicine`: Name or alias of the medicine.
  - `time`: Time taken (e.g., "08:15", "8:15 AM"). Defaults to current time if omitted.
- **Example Response**: "I've logged your BP tablet dose for Amma at 8:15 AM."

### 2. `whats_due(date: str | None = None)`
- **When to use**: When asked about today's medicine progress or schedule (e.g., "Did Amma take her medicines today?", "What's pending for today?").
- **Parameters**:
  - `date`: Optional date in `YYYY-MM-DD` format. Defaults to today.
- **Spoken Rule**: Report what was taken with timestamps, and clearly highlight any pending or missed doses.

### 3. `check_supply()`
- **When to use**: When asking about pill counts or low medicine stock (e.g., "Are any pills running low?", "Check medicine supply").
- **Behavior**: If supply is 5 days or less, proactively prompt whether a refill should be ordered.

### 4. `request_refill(medicine: str, quantity: int | None = None, confirm: bool = False)`
- **When to use**: When requesting a medication refill from the mock pharmacy.
- **Confirmation Rule**:
  - Step 1: Call without `confirm=True`. Ask the user: "Should I place a mock pharmacy refill order for [quantity] pills of [medicine]? Please confirm yes to proceed."
  - Step 2: Only call with `confirm=True` after the user explicitly confirms ("Yes", "Confirm", "Place order").
- **Note**: The pharmacy order is a mock and must be identified as such.

### 5. `prepare_visit_brief(appointment_id: int | None = None)`
- **When to use**: When preparing for a doctor consultation (e.g., "Prepare me for Amma's cardiology visit on Monday").
- **Behavior**: Compiles 14-day history of doses, missed doses, logged symptoms, active medicines, and suggested doctor questions. Triggers the MCP Apps visual card (`ui://carecircle/brief`).

### 6. `care_summary(days: int = 14)`
- **When to use**: When asking for an overview of care adherence over time (e.g., "Give me a summary of the last 2 weeks").
- **Behavior**: Available to all roles including clinic viewers. Links to the MCP Apps visual card.

### 7. `add_note(text: str, kind: str = "general")`
- **When to use**: When a symptom or general observation is reported (e.g., "Amma felt dizzy this morning", "Note: walking 15 mins daily").
- **Behavior**: Automatically categorizes dizzy, headache, nausea, pain, or fever as `symptom`.

### 8. `add_appointment(doctor: str, specialty: str, starts_at: str)`
- **When to use**: When scheduling upcoming doctor visits.

### 9. `add_medication(name: str, dose: str, times: list[str], pills_left: int)`
- **When to use**: When adding a new prescription or supplement.

### 10. `forget(item_type: str, item_id: int | None = None, query: str | None = None, confirm: bool = False)`
- **When to use**: When asked to remove or delete a record (e.g., "Forget the note about her dizziness").
- **Confirmation Rule**:
  - Step 1: Locate item, preview text, and ask: "Are you sure you want to permanently delete [item preview]? Please confirm."
  - Step 2: Call with `confirm=True` only after confirmation. An immutable privacy audit log is written upon deletion.
