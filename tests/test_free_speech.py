"""Test Check 5: Free Speech Verification.

Verifies that the agent answers unscripted free speech questions:
1. 'Has Amma missed any medicines this week?' (Ravi, Caregiver)
2. 'What doctor appointments does Amma have coming up?' (Priya, Caregiver)
3. 'How many sugar tablets do I have left?' (Amma, Patient)
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

SERVER_PORT = 8000
CLIENT_PORT = 8001


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


def test_free_speech():
    print("=" * 65)
    print("Running Check 5: Free Speech & Real LLM Verification (3 Utterances)")
    print("=" * 65)

    ensure_services_running()

    # Read tokens
    tokens = json.loads((PROJECT_ROOT / "scripts" / ".tokens.json").read_text())

    with httpx.Client(timeout=30.0) as client:
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
                "keywords": ["adherence", "missed", "dose", "tablet", "%"],
            },
            {
                "speaker": "Priya",
                "token": tokens["Priya"],
                "query": "What doctor appointments does Amma have coming up?",
                "expected_tool": "prepare_visit_brief",
                "keywords": ["doctor", "appointment", "rao", "cardiology", "visit"],
            },
            {
                "speaker": "Amma",
                "token": tokens["Amma"],
                "query": "How many sugar tablets do I have left?",
                "expected_tool": "check_supply",
                "keywords": ["metformin", "sugar", "tablet", "pill", "supply", "days", "left"],
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

            assert tool == tc["expected_tool"], f"Expected tool {tc['expected_tool']}, got {tool}"
            assert any(kw in spoken.lower() for kw in tc["keywords"]), f"Spoken reply missing expected keywords: {spoken}"

        print("\n" + "=" * 65)
        print("ALL 3 UNPROMPTED FREE SPEECH SENTENCES PASSED SUCCESSFULLY!")
        print("=" * 65)


if __name__ == "__main__":
    test_free_speech()
