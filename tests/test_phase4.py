"""Phase 4 verification test suite for MCP Apps Card integration.

Tests:
  1. resources/list includes ui://carecircle/brief
  2. resources/read returns the HTML brief card with MIME text/html;profile=mcp-app
  3. tools/list shows care_summary and prepare_visit_brief linked via _meta.ui.resourceUri
  4. Content audit: self-contained HTML (no external network calls, includes refill button & postMessage)
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

from server.main import app

TEST_PORT = 8993
SERVER_URL = f"http://127.0.0.1:{TEST_PORT}/mcp"


class TestServer:
    def __init__(self, port: int = TEST_PORT):
        self.port = port
        self.server = None
        self.thread = None

    def start(self):
        os.environ["ALLOWED_ORIGINS"] = "http://localhost:8000,http://127.0.0.1:8993"
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        time.sleep(1.2)

    def stop(self):
        if self.server:
            self.server.should_exit = True


async def run_tests():
    print("=" * 60)
    print("Starting CareCircle Phase 4 Automated Verification Suite")
    print("=" * 60)

    server = TestServer(TEST_PORT)
    server.start()

    tokens_file = PROJECT_ROOT / "scripts" / ".tokens.json"
    tokens = json.loads(tokens_file.read_text())
    priya_token = tokens["Priya"]

    headers = {
        "Authorization": f"Bearer {priya_token}",
        "Origin": f"http://127.0.0.1:{TEST_PORT}",
    }

    async with httpx2.AsyncClient(headers=headers) as http_c:
        async with streamable_http_client(SERVER_URL, http_client=http_c) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # ── Test 1: resources/list ─────────────────────────────
                print("\n[Test 1] resources/list — UI Resource Discovery")
                res_list = await session.list_resources()
                resource_uris = [str(r.uri) for r in res_list.resources]
                print(f"  Discovered resources: {resource_uris}")
                assert "ui://carecircle/brief" in resource_uris, "ui://carecircle/brief not found in resources/list"
                print("  [PASS] ui://carecircle/brief successfully listed")

                # ── Test 2: resources/read ─────────────────────────────
                print("\n[Test 2] resources/read — Fetching MCP Apps Card")
                content = await session.read_resource("ui://carecircle/brief")
                assert len(content.contents) > 0, "No resource contents returned"
                item = content.contents[0]
                assert item.mime_type == "text/html;profile=mcp-app", f"Wrong MIME type: {item.mime_type}"
                html_text = item.text or ""
                assert "<!DOCTYPE html>" in html_text, "Content is not HTML"
                assert "CareCircle" in html_text, "HTML missing CareCircle branding"
                assert "btn-refill" in html_text, "HTML missing Request Refill button"
                print(f"  [PASS] Card fetched successfully ({len(html_text)} bytes, MIME: {item.mime_type})")

                # ── Test 3: tools/list _meta Linkage ───────────────────
                print("\n[Test 3] tools/list — Tool _meta UI Linkage")
                tools_res = await session.list_tools()
                tools_by_name = {t.name: t for t in tools_res.tools}

                # Check care_summary
                assert "care_summary" in tools_by_name
                summary_tool = tools_by_name["care_summary"]
                assert summary_tool.meta is not None, "care_summary tool missing meta"
                assert summary_tool.meta.get("ui", {}).get("resourceUri") == "ui://carecircle/brief"
                print("  [PASS] care_summary linked to ui://carecircle/brief via meta.ui.resourceUri")

                # Check prepare_visit_brief
                assert "prepare_visit_brief" in tools_by_name
                brief_tool = tools_by_name["prepare_visit_brief"]
                assert brief_tool.meta is not None, "prepare_visit_brief tool missing meta"
                assert brief_tool.meta.get("ui", {}).get("resourceUri") == "ui://carecircle/brief"
                print("  [PASS] prepare_visit_brief linked to ui://carecircle/brief via meta.ui.resourceUri")

                # ── Test 4: Content Audit (No external calls) ──────────
                print("\n[Test 4] Content Audit — Self-Contained Integrity")
                assert "http://" not in html_text and "https://" not in html_text, "Found external network URLs in HTML!"
                assert "window.parent.postMessage" in html_text, "postMessage communication handler missing in HTML"
                print("  [PASS] HTML card is completely self-contained with zero external network dependencies")

    print("\n" + "=" * 60)
    print("ALL PHASE 4 TESTS PASSED SUCCESSFULLY!")
    print("=" * 60)

    server.stop()


if __name__ == "__main__":
    asyncio.run(run_tests())
