"""Test Check 5: Free Speech Verification with Dynamic SQLite Assertions.

Verifies that the agent answers unscripted free speech questions:
1. 'Has Amma missed any medicines this week?' (Ravi, Caregiver)
2. 'What doctor appointments does Amma have coming up?' (Priya, Caregiver)
3. 'How many sugar tablets do I have left?' (Amma, Patient)

Strictly compares against live SQLite database queries, removes hardcoded constants,
and includes a changed-data check to prove answers adapt dynamically to real data.
"""

from __future__ import annotations

import json
import socket
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DB_PATH = PROJECT_ROOT / "carecircle.db"
SERVER_PORT = 8000
CLIENT_PORT = 8001
IST = timezone(timedelta(hours=5, minutes=30))


def is_port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def ensure_services_running():
    if not is_port_open(SERVER_PORT):
        from server.main import app as server_app
        import uvicorn
        th1 = threading.Thread(
            target=lambda: uvicorn.run(server_app, host="127.0.0.1", port=SERVER_PORT, log_level="warning"),
            daemon=True,
        )
        th1.start()
        time.sleep(1.5)

    if not is_port_open(CLIENT_PORT):
        from client.app import app as client_app
        import uvicorn
        th2 = threading.Thread(
            target=lambda: uvicorn.run(client_app, host="127.0.0.1", port=CLIENT_PORT, log_level="warning"),
            daemon=True,
        )
        th2.start()
        time.sleep(1.5)


def get_db_facts(days: int = 7) -> dict:
    """Compute expected answer facts directly from SQLite."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Patient
        cur.execute("SELECT id, name FROM people WHERE role = 'patient' LIMIT 1")
        patient = cur.fetchone()
        patient_id = patient["id"]

        # Past N days dose logs and adherence
        now = datetime.now(IST)
        start_str = (now - timedelta(days=days)).strftime("%Y-%m-%d")

        cur.execute("SELECT * FROM medications WHERE person_id = ?", (patient_id,))
        meds = cur.fetchall()

        cur.execute(
            "SELECT dl.*, m.name as med_name FROM dose_logs dl "
            "JOIN medications m ON dl.medication_id = m.id "
            "WHERE dl.taken_at >= ?",
            (start_str,),
        )
        logs = cur.fetchall()

        total_expected = sum(int(len(json.loads(m["times_json"])) * days) for m in meds)
        total_taken = len(logs)
        total_missed = max(0, total_expected - total_taken)

        missed_meds = []
        for m in meds:
            t_count = len(json.loads(m["times_json"]))
            m_exp = t_count * days
            m_taken = sum(1 for l in logs if l["medication_id"] == m["id"])
            if m_taken < m_exp:
                missed_meds.append({
                    "name": m["name"],
                    "missed_count": m_exp - m_taken,
                })

        # Next upcoming appointment
        cur.execute(
            "SELECT doctor, specialty, starts_at FROM appointments "
            "WHERE person_id = ? ORDER BY starts_at ASC LIMIT 1",
            (patient_id,),
        )
        appt = cur.fetchone()
        appt_doctor = appt["doctor"] if appt else ""
        appt_specialty = appt["specialty"] if appt else ""
        appt_starts_at = appt["starts_at"] if appt else ""
        appt_dt = datetime.fromisoformat(appt_starts_at) if appt_starts_at else None

        # Metformin pill count
        cur.execute(
            "SELECT pills_left FROM medications WHERE person_id = ? AND name LIKE '%Metformin%'",
            (patient_id,),
        )
        metformin_row = cur.fetchone()
        metformin_pills = metformin_row["pills_left"] if metformin_row else 0

        return {
            "patient_name": patient["name"],
            "total_missed": total_missed,
            "missed_meds": missed_meds,
            "appt_doctor": appt_doctor,
            "appt_specialty": appt_specialty,
            "appt_dt": appt_dt,
            "metformin_pills": metformin_pills,
        }


def number_in_text(num: int, text: str) -> bool:
    """Check if number as digit or word exists in text."""
    words = {
        0: ("0", "zero", "no"),
        1: ("1", "one"),
        2: ("2", "two"),
        3: ("3", "three"),
        4: ("4", "four"),
        5: ("5", "five"),
        6: ("6", "six"),
        7: ("7", "seven"),
        8: ("8", "eight"),
        9: ("9", "nine"),
        10: ("10", "ten"),
    }
    candidates = words.get(num, (str(num),))
    return any(c in text.lower() for c in candidates)


def test_free_speech():
    print("=" * 65)
    print("Running Check 5: Free Speech & Real LLM Verification (3 Utterances)")
    print("=" * 65)

    from scripts.seed import seed
    seed()

    ensure_services_running()

    # Query initial database state dynamically
    facts = get_db_facts(days=7)
    print(f"Dynamically computed DB Facts for {facts['patient_name']}:")
    print(f"  Expected Missed Doses (past 7 days): {facts['total_missed']}")
    for m in facts["missed_meds"]:
        print(f"    - {m['name']}: {m['missed_count']} missed")
    if facts["appt_dt"]:
        print(f"  Next Appointment: {facts['appt_doctor']} ({facts['appt_specialty']}) on {facts['appt_dt'].strftime('%A, %B %d at %I:%M %p')}")
    print(f"  Metformin Pills in Stock: {facts['metformin_pills']}")
    print("-" * 65)

    # Read tokens
    tokens = json.loads((PROJECT_ROOT / "scripts" / ".tokens.json").read_text())

    with httpx.Client(timeout=120.0) as client:
        # Check provider metadata
        try:
            info = client.get(f"http://127.0.0.1:{CLIENT_PORT}/api/provider-info").json()
            print(f"Active Provider: {info.get('provider_name')}")
            print(f"Model:           {info.get('provider_model')}")
            print(f"MCP Spec:        {info.get('mcp_spec')}")
        except Exception as e:
            print(f"Warning: could not fetch provider info: {e}")

        test_cases = [
            {
                "speaker": "Ravi",
                "token": tokens["Ravi"],
                "query": "Has Amma missed any medicines this week?",
                "expected_tool": "care_summary",
            },
            {
                "speaker": "Priya",
                "token": tokens["Priya"],
                "query": "What doctor appointments does Amma have coming up?",
                "expected_tool": "prepare_visit_brief",
            },
            {
                "speaker": "Amma",
                "token": tokens["Amma"],
                "query": "How many sugar tablets do I have left?",
                "expected_tool": "check_supply",
            },
        ]

        for i, tc in enumerate(test_cases, 1):
            print("-" * 65)
            print(f"Test Sentence #{i}")
            print(f"Speaker: \"{tc['speaker']}\"")
            print(f"Utterance: \"{tc['query']}\"")

            res = client.post(
                f"http://127.0.0.1:{CLIENT_PORT}/api/chat",
                json={
                    "message": tc["query"],
                    "speaker": tc["speaker"],
                    "token": tc["token"],
                },
            )
            assert res.status_code == 200, f"Chat failed with HTTP {res.status_code}"
            data = res.json()

            spoken = data.get("spoken_reply", "")
            tool = data.get("tool_called")
            provider = data.get("provider_used", "Unknown")

            print(f"Provider Answering: {provider}")
            print(f"Tool Called:        {tool}")
            print(f"CareCircle Voice:   \"{spoken}\"")
            print(f"Card HTML Attached: {data.get('card_html') is not None}")

            # Strict assertions comparing against SQLite dynamically
            assert tool == tc["expected_tool"], f"Sentence #{i}: Expected tool {tc['expected_tool']}, got {tool}"

            if i == 1:
                # Assert sentence 1 matches dynamically computed DB missed count
                assert number_in_text(facts["total_missed"], spoken), (
                    f"Sentence #1 reply must contain the dynamic missed count ({facts['total_missed']}), got: '{spoken}'"
                )
                # Assert sentence 1 mentions missed medicine from DB
                for mm in facts["missed_meds"]:
                    key_part = "sugar" if "Metformin" in mm["name"] else "bp"
                    assert (key_part in spoken.lower() or mm["name"].split()[0].lower() in spoken.lower()), (
                        f"Sentence #1 reply must mention missed medicine ({mm['name']}), got: '{spoken}'"
                    )
                print(f"[PASS] Sentence #1 dynamically validated against SQLite missed count ({facts['total_missed']}) and medicines.")

            elif i == 2:
                # Assert sentence 2 matches dynamically computed DB appointment details
                doc_keyword = facts["appt_doctor"].lower().replace("dr.", "").replace("dr", "").strip()
                assert doc_keyword in spoken.lower(), (
                    f"Sentence #2 reply must mention doctor ({facts['appt_doctor']}), got: '{spoken}'"
                )
                spec_stem = facts["appt_specialty"].lower().rstrip("y")
                assert spec_stem in spoken.lower(), (
                    f"Sentence #2 reply must mention specialty ({facts['appt_specialty']}), got: '{spoken}'"
                )
                day_name = facts["appt_dt"].strftime("%A").lower()
                day_num = str(facts["appt_dt"].day)
                month_name = facts["appt_dt"].strftime("%B").lower()
                assert any(d in spoken.lower() for d in (day_name, day_num, month_name)), (
                    f"Sentence #2 reply must contain appointment day ({day_name}) or date ({day_num} {month_name}), got: '{spoken}'"
                )
                print(f"[PASS] Sentence #2 dynamically validated against SQLite doctor ({facts['appt_doctor']}) and schedule ({day_name}).")

            elif i == 3:
                # Assert sentence 3 matches dynamically computed DB Metformin count
                assert number_in_text(facts["metformin_pills"], spoken), (
                    f"Sentence #3 reply must contain the dynamic Metformin count ({facts['metformin_pills']}), got: '{spoken}'"
                )
                assert ("sugar" in spoken.lower() or "metformin" in spoken.lower()), (
                    f"Sentence #3 reply must mention sugar tablet / Metformin, got: '{spoken}'"
                )
                print(f"[PASS] Sentence #3 dynamically validated against SQLite Metformin stock ({facts['metformin_pills']}).")

            time.sleep(3.0)

        # ── Item 3: Changed-Data Check ─────────────────────────────────────
        print("\n" + "=" * 65)
        print("Running Changed-Data Check: Modifying SQLite Data & Re-testing")
        print("=" * 65)

        # Log one extra missed dose by deleting 1 dose_log from past 7 days
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM dose_logs WHERE id = ("
                "  SELECT id FROM dose_logs WHERE taken_at >= date('now', '-7 days') ORDER BY taken_at DESC LIMIT 1"
                ")"
            )
            conn.commit()

        # Re-compute dynamic DB facts
        new_facts = get_db_facts(days=7)
        print(f"Modified SQLite State: Missed count changed from {facts['total_missed']} -> {new_facts['total_missed']}")
        assert new_facts["total_missed"] != facts["total_missed"], "Failed to modify SQLite missed count!"

        # Rerun Sentence 1
        print("\nRerunning Sentence 1 with changed SQLite data...")
        res_changed = client.post(
            f"http://127.0.0.1:{CLIENT_PORT}/api/chat",
            json={
                "message": "Has Amma missed any medicines this week?",
                "speaker": "Ravi",
                "token": tokens["Ravi"],
            },
        )
        assert res_changed.status_code == 200
        spoken_changed = res_changed.json().get("spoken_reply", "")
        print(f"CareCircle Voice (Updated Data): \"{spoken_changed}\"")

        # Assert answer changed to match the new database count
        assert number_in_text(new_facts["total_missed"], spoken_changed), (
            f"Expected answer to update to new missed count ({new_facts['total_missed']}), got: '{spoken_changed}'"
        )
        print(f"[PASS] PROVEN: CareCircle Voice updated dynamically to reflect new missed count ({new_facts['total_missed']})!")

        # Reseed database cleanly back to demo state
        print("\n[Reseeding database back to clean demo state...]")
        from scripts.seed import seed
        seed()
        reseeded_facts = get_db_facts(days=7)
        assert reseeded_facts["total_missed"] == facts["total_missed"], "Reseed failed to restore count!"
        print(f"[PASS] Database cleanly reseeded. Missed count restored to {reseeded_facts['total_missed']}.")

    print("\n" + "=" * 65)
    print("ALL DYNAMIC SQLITE FREE SPEECH & CHANGED-DATA TESTS PASSED 100%!")
    print("=" * 65)


if __name__ == "__main__":
    test_free_speech()
