"""Phase 3 verification test suite for CareCircle MCP Server.

Tests:
  1. Tool 7: check_supply (identifies BP tablet low supply)
  2. Tool 8: request_refill - Step 1: Elicitation / confirmation prompt
  3. Tool 8: request_refill - Step 2: Confirmed execution of mock pharmacy order
  4. Tool 9: prepare_visit_brief (14-day history, missed doses, symptoms, doctor questions)
  5. Tool 10: forget - Step 1: Elicitation / confirmation prompt
  6. Tool 10: forget - Step 2: Confirmed permanent deletion & privacy audit log
  7. Role checks: Viewer blocked from request_refill and forget
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

from server.db import fetch_all, fetch_one
from server.main import app

TEST_PORT = 8992
SERVER_URL = f"http://127.0.0.1:{TEST_PORT}/mcp"


class TestServer:
    def __init__(self, port: int = TEST_PORT):
        self.port = port
        self.server = None
        self.thread = None

    def start(self):
        os.environ["ALLOWED_ORIGINS"] = "http://localhost:8000,http://127.0.0.1:8992"
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        time.sleep(1.2)

    def stop(self):
        if self.server:
            self.server.should_exit = True


async def call_mcp_tool(token: str, tool_name: str, args: dict) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Origin": f"http://127.0.0.1:{TEST_PORT}",
    }
    async with httpx2.AsyncClient(headers=headers) as http_c:
        async with streamable_http_client(SERVER_URL, http_client=http_c) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.call_tool(tool_name, args)
                assert len(res.content) > 0, "Empty tool result content"
                return json.loads(res.content[0].text)


async def run_tests():
    print("=" * 60)
    print("Starting CareCircle Phase 3 Automated Verification Suite")
    print("=" * 60)

    server = TestServer(TEST_PORT)
    server.start()

    tokens_file = PROJECT_ROOT / "scripts" / ".tokens.json"
    tokens = json.loads(tokens_file.read_text())
    ravi_token = tokens["Ravi"]
    priya_token = tokens["Priya"]
    clinic_token = tokens["Dr. Rao's Clinic"]

    # ── Test 1: check_supply ───────────────────────────────────────────
    print("\n[Test 1] Tool: check_supply")
    result = await call_mcp_tool(ravi_token, "check_supply", {})
    print(f"  Spoken response: \"{result['spoken_response']}\"")
    assert result["data"]["refill_needed"] is True
    assert any(m["is_low"] for m in result["data"]["medications"])
    print("  [PASS] Successfully identified medication with low stock")

    # ── Test 2: request_refill - Step 1 (Confirmation Prompt) ──────────
    print("\n[Test 2] Tool: request_refill (Step 1 - Confirmation Prompt)")
    result = await call_mcp_tool(ravi_token, "request_refill", {"medicine": "BP tablet"})
    print(f"  Spoken response: \"{result['spoken_response']}\"")
    assert result.get("confirmation_required") is True
    assert "confirm" in result["spoken_response"].lower() or "should i place" in result["spoken_response"].lower()
    print("  [PASS] Server asked for explicit confirmation before placing order")

    # ── Test 3: request_refill - Step 2 (Confirmed Order Execution) ────
    print("\n[Test 3] Tool: request_refill (Step 2 - Confirmed Execution)")
    result = await call_mcp_tool(ravi_token, "request_refill", {"medicine": "BP tablet", "confirm": True})
    print(f"  Spoken response: \"{result['spoken_response']}\"")
    assert result["data"]["status"] == "placed"
    assert result["data"]["is_mock"] is True
    assert "MOCK-" in result["spoken_response"]

    # Verify order in database
    order = fetch_one("SELECT * FROM refill_orders ORDER BY id DESC LIMIT 1")
    assert order is not None
    assert "placed" in order["status"]
    print(f"  [PASS] Refill order placed in DB (ID: {order['id']}, Status: {order['status']})")

    # ── Test 4: prepare_visit_brief ────────────────────────────────────
    print("\n[Test 4] Tool: prepare_visit_brief")
    result = await call_mcp_tool(priya_token, "prepare_visit_brief", {})
    print(f"  Spoken response: \"{result['spoken_response']}\"")
    brief_data = result["data"]
    assert "Dr. Rao" in brief_data["appointment"]["doctor"]
    assert "Cardiology" in brief_data["appointment"]["specialty"]
    assert brief_data["adherence"]["doses_taken"] > 0
    assert len(brief_data["symptoms"]) > 0
    assert len(brief_data["questions_to_ask"]) > 0
    print(f"  [PASS] Generated doctor visit brief for Dr. Rao ({len(brief_data['questions_to_ask'])} questions prepared)")

    # ── Test 5: forget - Step 1 (Confirmation Prompt) ──────────────────
    print("\n[Test 5] Tool: forget (Step 1 - Confirmation Prompt)")
    result = await call_mcp_tool(ravi_token, "forget", {"item_type": "note", "query": "dizziness"})
    print(f"  Spoken response: \"{result['spoken_response']}\"")
    assert result.get("confirmation_required") is True
    assert "dizzy" in result["data"]["preview"].lower() or "dizziness" in result["data"]["preview"].lower()
    print("  [PASS] Server identified dizziness note and prompted for confirmation")

    # ── Test 6: forget - Step 2 (Confirmed Deletion & Privacy Log) ─────
    print("\n[Test 6] Tool: forget (Step 2 - Confirmed Deletion & Privacy Log)")
    note_id = result["data"]["item_id"]
    result = await call_mcp_tool(ravi_token, "forget", {"item_type": "note", "item_id": note_id, "confirm": True})
    print(f"  Spoken response: \"{result['spoken_response']}\"")
    assert result["data"]["deleted"] is True
    assert result["data"]["privacy_log_recorded"] is True

    # Verify deleted from notes table
    deleted_note = fetch_one("SELECT * FROM notes WHERE id = ?", (note_id,))
    assert deleted_note is None, "Note was not deleted from DB"

    # Verify privacy log entry created
    log_entry = fetch_one("SELECT * FROM privacy_log WHERE item_type = 'note' AND item_id = ?", (note_id,))
    assert log_entry is not None, "Privacy log entry was not found"
    assert log_entry["action"] == "delete"
    print(f"  [PASS] Note permanently erased and privacy log entry recorded (Audit ID: {log_entry['id']})")

    # ── Test 7: Viewer Role Gate Enforcement ───────────────────────────
    print("\n[Test 7] Role Gate Enforcement (Viewer blocked from refill/forget)")
    refill_res = await call_mcp_tool(clinic_token, "request_refill", {"medicine": "BP tablet", "confirm": True})
    assert "Only caregivers and patients" in refill_res["spoken_response"]
    print("  [PASS] Viewer blocked from request_refill")

    forget_res = await call_mcp_tool(clinic_token, "forget", {"item_type": "note", "query": "headache", "confirm": True})
    assert "Only caregivers and patients" in forget_res["spoken_response"]
    print("  [PASS] Viewer blocked from forget")

    print("\n" + "=" * 60)
    print("ALL PHASE 3 TESTS PASSED SUCCESSFULLY!")
    print("=" * 60)

    server.stop()


if __name__ == "__main__":
    asyncio.run(run_tests())
