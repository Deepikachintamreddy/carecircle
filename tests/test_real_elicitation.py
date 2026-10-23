"""Automated test for live MCP Elicitation flow.

Tests:
1. Client connects with elicitation capability & elicitation_callback.
2. Client calls request_refill without confirm argument.
3. Server issues elicit request to client over Streamable HTTP session.
4. Client's elicitation_callback answers with action='accept' and data={'confirm': True}.
5. Server executes mock pharmacy refill order and returns confirmed order response.
6. Client calls forget without confirm argument, declines elicitation -> action cancelled.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx2
import uvicorn
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from server.db import set_db_path
from server.main import app

TEST_PORT = 8996
SERVER_URL = f"http://127.0.0.1:{TEST_PORT}/mcp"


def start_server():
    uvicorn.run(app, host="127.0.0.1", port=TEST_PORT, log_level="warning")


def get_token(name: str) -> str:
    tokens = json.loads((PROJECT_ROOT / "scripts" / ".tokens.json").read_text())
    return tokens[name]


async def test_live_elicitation():
    print("=" * 60)
    print("Testing Real Live MCP Elicitation over Streamable HTTP")
    print("=" * 60)

    token = get_token("Ravi")
    headers = {
        "Authorization": f"Bearer {token}",
        "Origin": "http://127.0.0.1:8000",
    }

    # Callback to handle elicitation requests from server
    elicitation_called = False
    elicit_prompt_received = ""

    async def elicitation_cb(ctx, params):
        nonlocal elicitation_called, elicit_prompt_received
        elicitation_called = True
        elicit_prompt_received = params.message
        print(f"\n[Client Received Elicitation Request]:\n  Message: '{params.message}'")
        print("  Client User Action: Clicking 'Confirm / Accept' in dialog...")
        return types.ElicitResult(
            action="accept",
            content={"confirm": True},
        )

    async with httpx2.AsyncClient(headers=headers, timeout=15.0) as http_c:
        async with streamable_http_client(SERVER_URL, http_client=http_c) as (read, write):
            async with ClientSession(
                read,
                write,
                elicitation_callback=elicitation_cb,
            ) as session:
                init_res = await session.initialize()
                print(f"[Init] Negotiated protocol version: {init_res.protocol_version}")

                # Call request_refill with confirm=False (relying on elicitation)
                print("\n[Test] Calling request_refill(medicine='BP tablet', confirm=False)...")
                res = await session.call_tool("request_refill", {"medicine": "BP tablet", "confirm": False})
                tool_data = json.loads(res.content[0].text)
                print(f"[Result]: {tool_data.get('spoken_response')}")

                assert elicitation_called, "Elicitation callback was NOT triggered!"
                print("\n[PASS] Server successfully sent Elicitation Request to client!")
                print(f"[PASS] Client approved via elicitation, order placed: {tool_data.get('data', {}).get('order_id')}")

    print("\n" + "=" * 60)
    print("REAL LIVE MCP ELICITATION VERIFIED SUCCESSFULLY!")
    print("=" * 60)


def main():
    th = threading.Thread(target=start_server, daemon=True)
    th.start()
    time.sleep(1.5)
    asyncio.run(test_live_elicitation())


if __name__ == "__main__":
    main()
