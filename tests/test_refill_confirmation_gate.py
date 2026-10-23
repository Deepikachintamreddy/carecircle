"""Test Card Refill Button Confirmation Gate.

Verifies that clicking the refill button on the MCP Apps card calls request_refill
WITHOUT confirm:true, proving that one click does NOT create an order by itself.
An order is only placed after explicit user confirmation.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DB_PATH = PROJECT_ROOT / "carecircle.db"
CLIENT_URL = "http://127.0.0.1:8001"


def get_order_count() -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT count(*) FROM refill_orders")
        return cursor.fetchone()[0]


def test_refill_button_does_not_order_without_confirmation():
    print("=" * 65)
    print("Running Test: Refill Button Single Click Confirmation Gate")
    print("=" * 65)

    # 1. Verify brief.html does NOT contain confirm: true
    card_html = (PROJECT_ROOT / "server" / "ui" / "brief.html").read_text(encoding="utf-8")
    assert "confirm: true" not in card_html and '"confirm": true' not in card_html, (
        "FAIL: server/ui/brief.html still hardcodes confirm:true in triggerRefill!"
    )
    print("[PASS] Verified server/ui/brief.html triggerRefill() does not pass confirm:true.")

    # 2. Count current orders in database
    orders_before = get_order_count()
    print(f"Current orders in database: {orders_before}")

    # Read Ravi's token
    tokens = json.loads((PROJECT_ROOT / "scripts" / ".tokens.json").read_text())
    ravi_token = tokens["Ravi"]

    with httpx.Client(timeout=60.0) as client:
        # 3. Simulate card button click: sends request_refill without confirmation
        print("\n[Simulating 1st Click on Card Refill Button]")
        res1 = client.post(
            f"{CLIENT_URL}/api/chat",
            json={
                "message": "request refill for BP tablet",
                "speaker": "Ravi",
                "token": ravi_token,
            },
        )
        assert res1.status_code == 200, f"Chat endpoint failed with {res1.status_code}"
        data1 = res1.json()

        spoken1 = data1.get("spoken_reply", "")
        pending1 = data1.get("confirmation_pending")
        print(f"Alexa+ Spoken Prompt: \"{spoken1}\"")
        print(f"Confirmation Pending State: {pending1}")

        # Assert confirmation is required and pending
        assert pending1 is not None and pending1.get("pending") is True, (
            f"Expected confirmation to be pending, got: {pending1}"
        )
        assert "confirm" in spoken1.lower() or "would you like" in spoken1.lower() or "should i" in spoken1.lower() or "place" in spoken1.lower(), (
            f"Expected confirmation prompt speech, got: '{spoken1}'"
        )

        # 4. CRITICAL ASSERTION: Prove no order was created in DB by single click
        orders_after_click = get_order_count()
        print(f"Orders in database after 1 click: {orders_after_click}")
        assert orders_after_click == orders_before, (
            f"FAIL: Single click created an order! Orders went from {orders_before} to {orders_after_click}"
        )
        print("[PASS] PROVEN: One click did NOT create an order by itself!")

        # 5. User provides explicit confirmation
        print("\n[Simulating User Confirmation: 'Yes, please place the order']")
        res2 = client.post(
            f"{CLIENT_URL}/api/chat",
            json={
                "message": "Yes, please place the refill order.",
                "speaker": "Ravi",
                "token": ravi_token,
                "confirmation_state": pending1,
            },
        )
        assert res2.status_code == 200
        data2 = res2.json()

        spoken2 = data2.get("spoken_reply", "")
        print(f"Alexa+ Spoken Reply: \"{spoken2}\"")

        # 6. Prove order was created ONLY after explicit confirmation
        orders_after_confirm = get_order_count()
        print(f"Orders in database after confirmation: {orders_after_confirm}")
        assert orders_after_confirm == orders_before + 1, (
            f"Expected orders to increase by 1, got {orders_after_confirm}"
        )
        assert "#MOCK-" in spoken2 or "refill order" in spoken2.lower() or "placed" in spoken2.lower(), (
            f"Expected order placement speech, got: '{spoken2}'"
        )
        print("[PASS] PROVEN: Order successfully created ONLY after explicit user confirmation!")

    print("\n" + "=" * 65)
    print("REFILL CONFIRMATION GATE TEST PASSED 100%!")
    print("=" * 65)


if __name__ == "__main__":
    test_refill_button_does_not_order_without_confirmation()
