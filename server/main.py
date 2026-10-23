"""CareCircle MCP Server — main entry point.

Runs an MCP server implementing spec 2025-11-25 over Streamable HTTP at /mcp.
Enforces Origin verification, Bearer Token authentication, and role checks.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure project root on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

import uvicorn
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from mcp.server import MCPServer
from server.db import init_db, set_db_path
from server.security import authenticate, check_origin
from server.tools.phase2 import (
    add_appointment,
    add_medication,
    add_note,
    care_summary,
    log_dose,
    whats_due,
)
from server.tools.phase3 import (
    check_supply,
    forget,
    prepare_visit_brief,
    request_refill,
)

# ── Database Init ─────────────────────────────────────────────────────
db_path = os.environ.get("DATABASE_PATH", str(PROJECT_ROOT / "carecircle.db"))
set_db_path(db_path)
init_db()


# ── MCP Server Instance ───────────────────────────────────────────────
mcp = MCPServer("CareCircle")

# UI Resource: ui://carecircle/brief (MCP Apps spec 2026-01-26)
@mcp.resource("ui://carecircle/brief", mime_type="text/html;profile=mcp-app")
def brief_card() -> str:
    """Return the visual CareCircle Brief Card HTML."""
    html_file = PROJECT_ROOT / "server" / "ui" / "brief.html"
    return html_file.read_text(encoding="utf-8")


# Register Phase 2 & 3 tools (All 10 tools)
mcp.tool()(log_dose)
mcp.tool()(whats_due)
mcp.tool()(add_medication)
mcp.tool()(add_note)
mcp.tool()(add_appointment)
# Linked to MCP Apps Card via _meta.ui.resourceUri
mcp.tool(meta={"ui": {"resourceUri": "ui://carecircle/brief", "visibility": ["model", "app"]}})(care_summary)
mcp.tool()(check_supply)
mcp.tool()(request_refill)
# Linked to MCP Apps Card via _meta.ui.resourceUri
mcp.tool(meta={"ui": {"resourceUri": "ui://carecircle/brief", "visibility": ["model", "app"]}})(prepare_visit_brief)
mcp.tool()(forget)




# ── Security Middleware ───────────────────────────────────────────────
class SecurityMiddleware(BaseHTTPMiddleware):
    """Enforces Origin validation and Bearer Token authentication on /mcp."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        # Health check endpoint
        if request.url.path == "/health":
            return JSONResponse({"status": "healthy", "service": "CareCircle MCP Server"})

        # Only protect /mcp endpoints
        if request.url.path.startswith("/mcp"):
            origin = request.headers.get("origin")

            # 1. Validate Origin header
            if not check_origin(origin):
                return Response("Forbidden: Invalid Origin header", status_code=403)

            # 2. Handle CORS preflight
            if request.method == "OPTIONS":
                response = Response(status_code=200)
                if origin:
                    response.headers["Access-Control-Allow-Origin"] = origin
                    response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
                    response.headers["Access-Control-Allow-Headers"] = (
                        "Authorization, Content-Type, Mcp-Session-Id, Mcp-Protocol-Version, Accept"
                    )
                    response.headers["Access-Control-Max-Age"] = "86400"
                return response

            # 3. Validate Bearer Token
            auth = request.headers.get("authorization", "")
            if not auth.lower().startswith("bearer "):
                return Response(
                    "Unauthorized: Missing or invalid Bearer token in Authorization header",
                    status_code=401,
                )

            token = auth[7:].strip()
            person = authenticate(token)
            if not person:
                return Response(
                    "Unauthorized: Bearer token is invalid or does not match any registered family member",
                    status_code=401,
                )

            # Store person in request state for access
            request.state.person = person

            # Call downstream MCP handler
            response = await call_next(request)

            # Attach CORS header if origin was validated
            if origin:
                response.headers["Access-Control-Allow-Origin"] = origin
            return response

        return await call_next(request)


def create_app() -> Starlette:
    """Create and configure the Starlette application."""
    streamable_app = mcp.streamable_http_app(streamable_http_path="/mcp")
    streamable_app.add_middleware(SecurityMiddleware)
    return streamable_app


app = create_app()


def run_server() -> None:
    """Run server with uvicorn."""
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    print(f"Starting CareCircle MCP Server on http://{host}:{port}/mcp ...")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run_server()
