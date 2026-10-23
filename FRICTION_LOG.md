# CareCircle Hackathon Friction Log

A factual record of real engineering hurdles, SDK anomalies, and workarounds encountered while building CareCircle on the official MCP Python SDK, MCP Apps specification, Streamable HTTP transport, and agent runtimes.

---

### 1. MCP Python SDK 2.x Renaming from `FastMCP` to `MCPServer`
- **Steps to Reproduce**:
  1. Install `mcp>=2.0.0` from PyPI.
  2. Attempt to import `from mcp.server.fastmcp import FastMCP` per documentation/examples written for SDK 1.x.
- **Expected Result**: Clean import or a backwards-compatible deprecation alias.
- **Actual Result**: `ModuleNotFoundError: No module named 'mcp.server.fastmcp'`. In addition, tool schemas changed camelCase properties (`t.inputSchema`) to snake_case (`t.input_schema`).
- **Workaround**: Migrated code directly to `from mcp.server import MCPServer`, using `@mcp.tool()` and `@mcp.resource()`.
- **Suggestion for Maintainers**: Retain a stub module in `mcp.server.fastmcp` with `from mcp.server.mcpserver import MCPServer as FastMCP` and a deprecation warning to avoid breaking introductory tutorials.

---

### 2. Python 3.14 Virtual Environment Binary ABI Incompatibility
- **Steps to Reproduce**:
  1. Transfer a project containing a `.venv` built on a developer machine with Python 3.14 (`cp314`) to a host running Python 3.13 (`cp313`).
  2. Attempt to run `.\.venv\Scripts\python.exe`.
- **Expected Result**: Executable launches or provides a clear notice about absolute path relocation.
- **Actual Result**: Failed with `did not find executable at 'C:\Users\...\Python314\python.exe': The system cannot find the path specified`. In addition, compiled C-extension wheels (`pydantic_core`, `cryptography`, `cffi`) failed ABI linking across Python versions.
- **Workaround**: Completely wiped the legacy `.venv` directory and rebuilt a clean virtual environment using the target system's active runtime (`py -3.13 -m venv .venv`), cleanly reinstalling all pinned dependencies from `requirements.txt`.
- **Suggestion for Maintainers**: Ensure `requirements.txt` includes pinned versions so clean virtual environments can be rebuilt in under 60 seconds on any machine.

---

### 3. Windows Terminal CP1252 Unicode Glyph Crashes
- **Steps to Reproduce**:
  1. Print Unicode checkmark symbols (e.g. `✓` `\u2713` or `→` `\u2192`) to stdout in PowerShell / Command Prompt on Windows.
- **Expected Result**: Windows terminal displays glyphs or replaces with fallback question marks.
- **Actual Result**: Python crashes with `UnicodeEncodeError: 'charmap' codec can't encode character '\u2713' in position ...: character maps to <undefined>`.
- **Workaround**: Replaced all stdout terminal glyphs with ASCII tags (`[PASS]`, `[FAIL]`, `[Init]`, `->`) across all test suites and seed scripts.
- **Suggestion for Maintainers**: Default test runners and CLI tools to `sys.stdout.reconfigure(encoding='utf-8')` on Windows.

---

### 4. Streamable HTTP Transport Header Propagation
- **Steps to Reproduce**:
  1. Send an HTTP request with `Authorization: Bearer <token>` to an MCP Server mounted at `/mcp` using `streamable_http_app()`.
  2. Inspect how the tool implementation can identify the authenticated caller.
- **Expected Result**: An official, documented API for extracting HTTP transport headers within tool execution contexts.
- **Actual Result**: Standard tool functions receive only tool arguments. Identity cannot be accepted as a tool argument because LLMs or clients could easily forge caller identity.
- **Workaround**: Discovered that injecting `ctx: Context` into the tool function allows access to `ctx.headers`, which exposes the Starlette `Headers` mapping (`authorization`, `origin`, `mcp-session-id`). Implemented Starlette middleware to validate tokens against SQLite upfront, and `get_caller(ctx)` inside tools to enforce role gates.
- **Suggestion for Maintainers**: Document `ctx.headers` and caller authentication patterns clearly in the official Streamable HTTP transport guide.

---

### 5. Dependency Conflict Between `strands-agents` and `mcp`
- **Steps to Reproduce**:
  1. Install `strands-agents 1.62.0` in a virtual environment with `mcp 2.3.0`.
- **Expected Result**: Package resolution succeeds.
- **Actual Result**: Pip resolver forced a downgrade of `mcp` from `2.3.0` to `2.1.1` (`mcp<2.2,>=1.23.0`).
- **Workaround**: Audited all `MCPServer` APIs (`streamable_http_app`, `@mcp.tool(meta=...)`, `@mcp.resource`) on `mcp==2.1.1`. Verified that all Streamable HTTP endpoints and MCP Apps metadata work identically on `2.1.1`.
- **Suggestion for Maintainers**: Loosen `mcp` upper-bound version constraints in `strands-agents` to support newer releases.

---

### 6. MCP Apps Specification (2026-01-26) Tool `_meta` Schema Evolution
- **Steps to Reproduce**:
  1. Associate an MCP tool with a visual UI resource per early tutorials using `_meta["ui/resourceUri"]`.
- **Expected Result**: Host recognizes the resource and renders the card.
- **Actual Result**: The finalized SEP-1865 specification deprecated flat keys in favor of nested metadata:
  ```json
  {
    "_meta": {
      "ui": {
        "resourceUri": "ui://carecircle/brief",
        "visibility": ["model", "app"]
      }
    }
  }
  ```
- **Workaround**: Updated tool registrations to `mcp.tool(meta={"ui": {"resourceUri": "ui://carecircle/brief", "visibility": ["model", "app"]}})` and served the card resource with MIME type `text/html;profile=mcp-app`.
- **Suggestion for Maintainers**: Provide a helper in FastMCP/MCPServer like `mcp.tool(ui_card="ui://...")` to reduce boilerplate.

---

### 7. MCP Spec 2025-11-25 Protocol Handshake & Dual Confirmation Path
- **Steps to Reproduce**:
  1. Send an `initialize` JSON-RPC request specifying `"protocolVersion": "2025-11-25"`.
  2. Implement tools requiring user confirmation (`request_refill`, `forget`) across both MCP Apps hosts (with modal dialogs) and simulated voice assistants (pure spoken voice).
- **Expected Result**: Handshake negotiates `2025-11-25`, and confirmation works across both modality types.
- **Actual Result**: Hosts with interactive UI support `ctx.elicit` (`AcceptedElicitation(data=...)`), while voice assistants without interactive dialog support fail or hang when modal elicitation is requested.
- **Workaround**: Implemented a dual-path confirmation architecture:
  - If client supports MCP elicitation, `ctx.elicit` prompts the user with an interactive confirmation modal.
  - If elicitation is unsupported or unanswered, the server cleanly falls back to a 2-step conversational voice prompt requiring `confirm=true` on the subsequent utterance.
- **Suggestion for Maintainers**: Standardize client capability negotiation for conversational elicitation fallbacks in the core specification.
