"""Verification script for MCP protocol version 2025-11-25 negotiation."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
import httpx
import uvicorn

from server.main import app
from server.db import set_db_path

PROJECT_ROOT = Path(__file__).resolve().parent
tokens_file = PROJECT_ROOT / "scripts" / ".tokens.json"
tokens = json.loads(tokens_file.read_text())
priya_token = tokens["Priya"]

# Start server in background thread on port 8995
server_thread = threading.Thread(
    target=uvicorn.run,
    args=(app,),
    kwargs={"host": "127.0.0.1", "port": 8995, "log_level": "warning"},
    daemon=True,
)
server_thread.start()
time.sleep(1.5)

# Run Check 2 as requested
r = httpx.post(
    "http://127.0.0.1:8995/mcp",
    headers={
        "Authorization": f"Bearer {priya_token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    },
    json={
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {"elicitation": {}},
            "clientInfo": {"name": "verify", "version": "0.1"},
        },
    },
)

print("STATUS:", r.status_code)
print("CONTENT-TYPE:", r.headers.get("content-type"))
print("SESSION ID:", r.headers.get("mcp-session-id"))
print("BODY:")
print(r.text[:1500])

# Verify protocolVersion in response
if "2025-11-25" in r.text:
    print("\n[VERIFICATION SUCCESS] Server negotiated protocolVersion: 2025-11-25")
else:
    print("\n[VERIFICATION FAILED] protocolVersion 2025-11-25 not found in response!")
