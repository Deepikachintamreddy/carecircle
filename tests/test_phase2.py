"""Phase 2 verification test suite for CareCircle MCP Server.

Tests:
  1. Origin check (disallowed origin -> 403)
  2. Token authentication (missing/invalid token -> 401)
  3. Role check (viewer role blocked from write tools)
  4. Tool 1: log_dose (patient logs BP tablet)
  5. Tool 2: whats_due (caregiver checks daily schedule)
  6. Tool 3: add_medication (caregiver adds medication)
  7. Tool 4: add_note (caregiver logs symptom note)
  8. Tool 5: add_appointment (caregiver logs cardiology appointment)
  9. Tool 6: care_summary (viewer clinic accesses care summary)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx2
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from server.db import fetch_all, fetch_one, set_db_path
from server.main import app
from server.security import hash_token

TEST_PORT = 8991
SERVER_URL = f"http://127.0.0.1:{TEST_PORT}/mcp"


def get_token_for_person(name: str) -> str:
    """Read seed demo tokens or query db for matching token."""
    # Read the token mapping from a local cache or generate/lookup from DB
    person = fetch_one("SELECT * FROM people WHERE name = ?", (name,))
    if not person:
        raise ValueError(f"Person {name} not found in database. Run seed script first.")
    # Return known seed tokens if saved, or we can look up from tokens.json
    tokens_file = PROJECT_ROOT / "scripts" / ".tokens.json"
    if tokens_file.exists():
        tokens = json.loads(tokens_file.read_text())
        if name in tokens:
            return tokens[name]
    raise ValueError(f"Token for {name} not found in .tokens.json")


class TestServer:
    def __init__(self, port: int = TEST_PORT):
        self.port = port
        self.server = None
        self.thread = None

    def start(self):
        os.environ["ALLOWED_ORIGINS"] = "http://localhost:8000,http://127.0.0.1:8991,http://allowed.test"
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        time.sleep(1.2)  # Give server time to bind

    def stop(self):
        if self.server:
            self.server.should_exit = True


async def run_tests():
    print("=" * 60)
    print("Starting CareCircle Phase 2 Automated Verification Suite")
    print("=" * 60)

    server = TestServer(TEST_PORT)
    server.start()

    tokens_file = PROJECT_ROOT / "scripts" / ".tokens.json"
    if not tokens_file.exists():
        print("ERROR: .tokens.json not found. Running seed script...")
        import scripts.seed
        scripts.seed.seed()

    tokens = json.loads(tokens_file.read_text())
    amma_token = tokens["Amma"]
    ravi_token = tokens["Ravi"]
    priya_token = tokens["Priya"]
    clinic_token = tokens["Dr. Rao's Clinic"]

    # ── Test 1: Origin Validation ──────────────────────────────────────
    print("\n[Test 1] Origin Header Validation")
    async with httpx2.AsyncClient() as client:
        # Disallowed origin -> 403
        resp = await client.post(
            SERVER_URL,
            headers={"Origin": "http://evil-site.com", "Authorization": f"Bearer {amma_token}"},
            content=b"{}",
        )
        assert resp.status_code == 403, f"Expected 403 for disallowed origin, got {resp.status_code}"
        print("  [PASS] Disallowed origin correctly returned HTTP 403 Forbidden")

        # Allowed origin -> passes origin check
        resp = await client.post(
            SERVER_URL,
            headers={"Origin": "http://127.0.0.1:8991"},
            content=b"{}",
        )
        assert resp.status_code == 401, f"Expected 401 (passed origin check, failed auth), got {resp.status_code}"
        print("  [PASS] Allowed origin passed origin check (proceeded to auth)")

    # ── Test 2: Token Authentication ──────────────────────────────────
    print("\n[Test 2] Bearer Token Authentication")
    async with httpx2.AsyncClient() as client:
        # Missing token -> 401
        resp = await client.post(SERVER_URL, content=b"{}")
        assert resp.status_code == 401, f"Expected 401 for missing token, got {resp.status_code}"
        print("  [PASS] Missing Authorization header returned HTTP 401")

        # Invalid token -> 401
        resp = await client.post(
            SERVER_URL,
            headers={"Authorization": "Bearer bad-token-12345"},
            content=b"{}",
        )
        assert resp.status_code == 401, f"Expected 401 for invalid token, got {resp.status_code}"
        print("  [PASS] Invalid bearer token returned HTTP 401")

    # ── Helper for MCP Session ─────────────────────────────────────────
    async def call_mcp_tool(token: str, tool_name: str, args: dict) -> dict:
        headers = {
            "Authorization": f"Bearer {token}",
            "Origin": "http://127.0.0.1:8991",
        }
        async with httpx2.AsyncClient(headers=headers) as http_c:
            async with streamable_http_client(SERVER_URL, http_client=http_c) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    res = await session.call_tool(tool_name, args)
                    assert len(res.content) > 0, "Empty tool result content"
                    raw_text = res.content[0].text
                    return json.loads(raw_text)

    # ── Test 3: Tool 1 - log_dose ──────────────────────────────────────
    print("\n[Test 3] Tool: log_dose")
    # Amma (patient) logs her BP tablet
    result = await call_mcp_tool(amma_token, "log_dose", {"medicine": "BP tablet", "time": "08:10"})
    print(f"  Spoken response: \"{result['spoken_response']}\"")
    assert "BP tablet" in result["data"]["medication_name"] or "Amlodipine" in result["data"]["medication_name"]
    assert "8:10" in result["spoken_response"]
    print("  [PASS] Amma successfully logged BP tablet dose at 8:10")

    # ── Test 4: Tool 2 - whats_due ─────────────────────────────────────
    print("\n[Test 4] Tool: whats_due")
    # Ravi (caregiver) checks what is due today
    result = await call_mcp_tool(ravi_token, "whats_due", {})
    print(f"  Spoken response: \"{result['spoken_response']}\"")
    assert "taken" in result["data"]
    assert len(result["data"]["taken"]) >= 1
    print("  [PASS] Ravi retrieved daily schedule showing logged and pending doses")

    # ── Test 5: Tool 3 - add_medication ────────────────────────────────
    print("\n[Test 5] Tool: add_medication")
    # Ravi adds a calcium supplement
    result = await call_mcp_tool(
        ravi_token,
        "add_medication",
        {
            "name": "Calcium 500mg",
            "dose": "500 mg",
            "times": "10:00",
            "pills_left": 45,
            "pills_per_day": 1,
            "aliases": ["calcium tablet", "calcium"],
        },
    )
    print(f"  Spoken response: \"{result['spoken_response']}\"")
    assert result["data"]["name"] == "Calcium 500mg"
    print("  [PASS] Ravi successfully added Calcium medication")

    # ── Test 6: Tool 4 - add_note ──────────────────────────────────────
    print("\n[Test 6] Tool: add_note")
    # Priya (caregiver) logs a symptom note about dizziness
    result = await call_mcp_tool(
        priya_token,
        "add_note",
        {"text": "Amma complained of mild dizziness when waking up this morning", "kind": "symptom"},
    )
    print(f"  Spoken response: \"{result['spoken_response']}\"")
    assert result["data"]["kind"] == "symptom"
    print("  [PASS] Priya recorded dizziness symptom note")

    # ── Test 7: Tool 5 - add_appointment ───────────────────────────────
    print("\n[Test 7] Tool: add_appointment")
    # Priya schedules cardiology follow-up
    result = await call_mcp_tool(
        priya_token,
        "add_appointment",
        {
            "doctor": "Rao",
            "specialty": "Cardiology",
            "starts_at": "2026-10-26T10:00:00+05:30",
        },
    )
    print(f"  Spoken response: \"{result['spoken_response']}\"")
    assert result["data"]["doctor"] == "Rao"
    print("  [PASS] Priya recorded Cardiology appointment")

    # ── Test 8: Tool 6 - care_summary ──────────────────────────────────
    print("\n[Test 8] Tool: care_summary")
    # Dr. Rao's Clinic (viewer) requests care summary
    result = await call_mcp_tool(clinic_token, "care_summary", {"days": 14})
    print(f"  Spoken response: \"{result['spoken_response']}\"")
    assert "adherence_percentage" in result["data"]
    assert "recent_symptoms" in result["data"]
    print(f"  [PASS] Clinic retrieved 14-day summary ({result['data']['adherence_percentage']}% adherence)")

    # ── Test 9: Role Gate Enforcement ──────────────────────────────────
    print("\n[Test 9] Role Gate Enforcement (Viewer blocked from write)")
    # Dr. Rao's Clinic (viewer) attempts to log a dose
    result = await call_mcp_tool(clinic_token, "log_dose", {"medicine": "BP tablet"})
    print(f"  Spoken response: \"{result['spoken_response']}\"")
    assert "Only patients and caregivers can log doses" in result["spoken_response"]
    assert "error" in result["data"]
    print("  [PASS] Viewer was blocked from mutating care records")

    print("\n" + "=" * 60)
    print("ALL PHASE 2 TESTS PASSED SUCCESSFULLY!")
    print("=" * 60)

    server.stop()


if __name__ == "__main__":
    asyncio.run(run_tests())
