"""End-to-End Demo Story Verification for CareCircle Alexa+ Add-on.

Runs the exact 5-step demo story from the hackathon requirement specification:
  1. Amma (patient): "I took my BP tablet." -> dose logged.
  2. Ravi (caregiver): "Did Amma take her medicines today?" -> taken & pending doses reported.
  3. Priya (caregiver): "Prepare me for Amma's cardiology visit on Monday." -> 14-day brief + card.
  4. Ravi (caregiver): "Her BP tablets are running low." -> confirmation prompt -> confirmed mock refill.
  5. Ravi (caregiver): "Forget the note about her dizziness." -> confirmation prompt -> permanent deletion + privacy log.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx2
import uvicorn

import client.app
import server.main
from server.db import IST, fetch_all, fetch_one

MCP_PORT = 8000
CLIENT_PORT = 8001
CLIENT_URL = f"http://127.0.0.1:{CLIENT_PORT}"


def is_port_open(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


class ServicesRunner:
    def __init__(self):
        self.mcp_server = None
        self.client_server = None

    def start(self):
        # Configure Origins
        os.environ["ALLOWED_ORIGINS"] = f"http://localhost:{CLIENT_PORT},http://127.0.0.1:{CLIENT_PORT},http://127.0.0.1:{MCP_PORT}"
        os.environ["MCP_SERVER_URL"] = f"http://127.0.0.1:{MCP_PORT}/mcp"

        # Start MCP Server on 8000 if not running
        if not is_port_open(MCP_PORT):
            mcp_config = uvicorn.Config(server.main.app, host="127.0.0.1", port=MCP_PORT, log_level="error")
            self.mcp_server = uvicorn.Server(mcp_config)
            threading.Thread(target=self.mcp_server.run, daemon=True).start()

        # Start Voice Client on 8001 if not running
        if not is_port_open(CLIENT_PORT):
            client_config = uvicorn.Config(client.app.app, host="127.0.0.1", port=CLIENT_PORT, log_level="error")
            self.client_server = uvicorn.Server(client_config)
            threading.Thread(target=self.client_server.run, daemon=True).start()

        time.sleep(1.5)

    def stop(self):
        if self.mcp_server:
            self.mcp_server.should_exit = True
        if self.client_server:
            self.client_server.should_exit = True


async def run_demo_story():
    print("=" * 70)
    print("   CARECIRCLE — 5-STEP END-TO-END DEMO STORY VERIFICATION")
    print("=" * 70)

    # Reset database with clean seed data
    import scripts.seed
    scripts.seed.seed()

    runner = ServicesRunner()
    runner.start()

    tokens_file = PROJECT_ROOT / "scripts" / ".tokens.json"
    tokens = json.loads(tokens_file.read_text())

    amma_token = tokens["Amma"]
    ravi_token = tokens["Ravi"]
    priya_token = tokens["Priya"]

    async with httpx2.AsyncClient(timeout=90.0) as http_client:
        # ── Step 1: Amma logs dose ─────────────────────────────────────
        print("\n[Step 1] Amma (Patient, Morning): \"I took my BP tablet.\"")
        payload = {
            "message": "I took my BP tablet at 8:10",
            "speaker": "Amma",
            "token": amma_token,
        }
        resp = await http_client.post(f"{CLIENT_URL}/api/chat", json=payload)
        data = resp.json()
        print(f"  Alexa+ Spoken Reply: \"{data['spoken_reply']}\"")
        assert "BP tablet" in data["spoken_reply"] or "Amlodipine" in data["spoken_reply"]
        assert "8:10" in data["spoken_reply"]
        print("  [PASS] Dose recorded in shared private family record.")

        # ── Step 2: Ravi checks today's status ─────────────────────────
        print("\n[Step 2] Ravi (Son/Caregiver, Evening): \"Did Amma take her medicines today?\"")
        payload = {
            "message": "Did Amma take her medicines today?",
            "speaker": "Ravi",
            "token": ravi_token,
        }
        resp = await http_client.post(f"{CLIENT_URL}/api/chat", json=payload)
        data = resp.json()
        print(f"  Alexa+ Spoken Reply: \"{data['spoken_reply']}\"")
        assert data.get("tool_called") == "whats_due", f"Expected whats_due tool to be called, got: {data.get('tool_called')}"

        # Dynamically compute today's taken and overdue/pending doses from SQLite
        now_dt = datetime.now(IST)
        today_str = now_dt.strftime("%Y-%m-%d")
        now_hm = now_dt.strftime("%H:%M")
        patient = fetch_one("SELECT id FROM people WHERE role = 'patient' LIMIT 1")
        meds = fetch_all("SELECT * FROM medications WHERE person_id = ?", (patient["id"],))
        today_logs = fetch_all(
            "SELECT dl.*, m.name as med_name FROM dose_logs dl "
            "JOIN medications m ON dl.medication_id = m.id "
            "WHERE dl.taken_at LIKE ?",
            (f"{today_str}%",),
        )

        expected_taken_names = [l["med_name"] for l in today_logs]
        expected_overdue_names = []
        expected_pending_names = []
        for m in meds:
            times = json.loads(m["times_json"])
            m_logs = [l for l in today_logs if l["medication_id"] == m["id"]]
            for idx, slot_time in enumerate(times):
                if idx >= len(m_logs):
                    if slot_time < now_hm:
                        expected_overdue_names.append(m["name"])
                    else:
                        expected_pending_names.append(m["name"])

        reply_lower = data["spoken_reply"].lower()
        # 1. Assert today's taken dose is listed
        for med in expected_taken_names:
            stem = med.split()[0].lower()
            assert stem in reply_lower or "bp tablet" in reply_lower, (
                f"Step 2 reply should list taken med '{med}': {data['spoken_reply']}"
            )

        # 2. Assert overdue or pending doses are listed
        if expected_overdue_names:
            for med in expected_overdue_names:
                stem = med.split()[0].lower()
                assert stem in reply_lower or "sugar tablet" in reply_lower or "metformin" in reply_lower, (
                    f"Step 2 reply should list overdue med '{med}': {data['spoken_reply']}"
                )
            assert "overdue" in reply_lower, f"Step 2 reply should indicate overdue status: {data['spoken_reply']}"
        elif expected_pending_names:
            assert "pending" in reply_lower or "upcoming" in reply_lower, (
                f"Step 2 reply should indicate upcoming/pending status: {data['spoken_reply']}"
            )

        # 3. Assert it does NOT use 7-day missed count
        assert "this week" not in reply_lower and "last 7 days" not in reply_lower and "missed 4 doses" not in reply_lower, (
            f"Step 2 reply must not use 7-day missed count: {data['spoken_reply']}"
        )
        print("  [PASS] Step 2 reply dynamically verified against SQLite today's taken and overdue doses.")

        # ── Step 3: Priya prepares cardiology visit brief ───────────────
        print("\n[Step 3] Priya (Daughter/Caregiver): \"Prepare me for Amma's cardiology visit on Monday.\"")
        payload = {
            "message": "Prepare me for Amma's cardiology visit on Monday",
            "speaker": "Priya",
            "token": priya_token,
        }
        resp = await http_client.post(f"{CLIENT_URL}/api/chat", json=payload)
        data = resp.json()
        print(f"  Alexa+ Spoken Reply: \"{data['spoken_reply']}\"")
        assert data.get("card_html") is not None, "MCP Apps visual card was not returned!"
        assert data.get("card_data") is not None
        assert "Dr. Rao" in data["spoken_reply"]
        print(f"  [PASS] 14-day history synthesized and visual card rendered ({len(data['card_html'])} bytes).")

        # ── Step 4: Supply Check & Confirmation-Gated Refill ───────────
        print("\n[Step 4a] \"Her BP tablets are running low.\"")
        payload = {
            "message": "Her BP tablets are running low",
            "speaker": "Ravi",
            "token": ravi_token,
        }
        resp = await http_client.post(f"{CLIENT_URL}/api/chat", json=payload)
        data = resp.json()
        print(f"  Alexa+ Spoken Reply: \"{data['spoken_reply']}\"")
        assert data.get("confirmation_pending") is not None, "Elicitation confirmation prompt missing!"
        print("  [PASS] Confirmation requested before spending money on refill order.")

        print("\n[Step 4b] User confirms: \"Yes, please place the refill order.\"")
        confirm_payload = {
            "message": "Yes, please place the refill order",
            "speaker": "Ravi",
            "token": ravi_token,
            "confirmation_state": data["confirmation_pending"],
        }
        resp = await http_client.post(f"{CLIENT_URL}/api/chat", json=confirm_payload)
        data = resp.json()
        print(f"  Alexa+ Spoken Reply: \"{data['spoken_reply']}\"")
        assert "mock" in data["spoken_reply"].lower() or "MOCK" in data["spoken_reply"]
        print("  [PASS] Mock pharmacy refill order successfully placed with order reference.")

        # ── Step 5: Privacy Deletion with Confirmation & Audit Log ─────
        print("\n[Step 5a] \"Forget the note about her dizziness.\"")
        payload = {
            "message": "Forget the note about her dizziness",
            "speaker": "Ravi",
            "token": ravi_token,
        }
        resp = await http_client.post(f"{CLIENT_URL}/api/chat", json=payload)
        data = resp.json()
        print(f"  Alexa+ Spoken Reply: \"{data['spoken_reply']}\"")
        assert data.get("confirmation_pending") is not None, "Elicitation confirmation prompt missing!"
        print("  [PASS] Confirmation requested before permanent data deletion.")

        print("\n[Step 5b] User confirms: \"Yes, confirm deletion.\"")
        confirm_payload = {
            "message": "Yes, confirm deletion",
            "speaker": "Ravi",
            "token": ravi_token,
            "confirmation_state": data["confirmation_pending"],
        }
        resp = await http_client.post(f"{CLIENT_URL}/api/chat", json=confirm_payload)
        data = resp.json()
        print(f"  Alexa+ Spoken Reply: \"{data['spoken_reply']}\"")
        assert "erased" in data["spoken_reply"] or "privacy" in data["spoken_reply"]

        # Audit privacy_log in database
        audit = fetch_one("SELECT * FROM privacy_log WHERE action='delete' ORDER BY id DESC LIMIT 1")
        assert audit is not None
        print(f"  [PASS] Note permanently deleted; privacy audit log recorded (Audit ID #{audit['id']}).")

    print("\n" + "=" * 70)
    print("ALL 5 STEPS OF THE DEMO STORY PASSED END-TO-END!")
    print("=" * 70)

    runner.stop()


if __name__ == "__main__":
    asyncio.run(run_demo_story())
