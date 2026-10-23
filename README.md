# CareCircle • Alexa+ Family Care Coordinator

[![MCP Spec](https://img.shields.io/badge/MCP%20Spec-2025--11--25-blue.svg)](https://modelcontextprotocol.io/specification/2025-11-25)
[![MCP Apps](https://img.shields.io/badge/MCP%20Apps-2026--01--26-purple.svg)](https://github.com/modelcontextprotocol/ext-apps)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python: 3.13](https://img.shields.io/badge/Python-3.13-blue.svg)](https://www.python.org/)

> **Track**: Alexa+  
> **Hackathon**: *"Build, Ship, Shape: Amazon Developer Hackathon"* (Devpost)  
> **Technology**: Self-hosted MCP Server implementing MCP specification **2025-11-25** over **Streamable HTTP** at `/mcp`, with **MCP Apps (2026-01-26)** interactive cards and a simulated Alexa+ voice client.

> [!WARNING]
> **Demo Only — Not Medical Advice**: CareCircle is an engineering proof-of-concept for family care coordination. It is for demo and hackathon evaluation purposes only and does NOT constitute medical advice, medical diagnosis, or clinical prescription guidance. All pharmacy orders placed through `request_refill` are simulated mocks.

---

## What is CareCircle?

**CareCircle** is an Alexa+ add-on (MCP Server) that turns Alexa into a shared, private care coordinator for a family caring for an aging parent.

Instead of fragmented WhatsApp messages, forgotten pill boxes, and stressful doctor visits:
1. **Single Private Source of Truth**: Keeps a shared, private record of medicines, doses taken, remaining stock, symptom notes, and appointments in SQLite.
2. **Cross-Session & Cross-Family Identity**: Identifies each family member from their authenticated bearer token—never from tool arguments.
3. **Safety & Confirmation Gates**: Any action that spends money (refill orders) or deletes data permanently (`forget`) enforces explicit confirmation before taking action.
4. **Interactive MCP Apps Visual Card**: Generates a doctor visit brief card (`ui://carecircle/brief`) showing 14-day adherence, logged symptoms, and smart doctor questions, complete with an interactive "Request Refill" button.
5. **Mock Pharmacy Refill**: Integrates with simulated pharmacy fulfillment (labeled clearly as a mock).

---

## Architecture Diagram

```mermaid
flowchart TD
    subgraph Client ["Simulated Alexa+ Voice Client (Port 8001)"]
        UI["Web Interface (Browser STT & Speech Synthesis)"]
        Host["MCP Apps Host (Sandboxed iframe)"]
        Agent["Voice Agent (Gemini via OpenAI-compatible API; keyword fallback offline)"]
    end

    subgraph Server ["CareCircle MCP Server (Port 8000)"]
        StreamHTTP["Streamable HTTP Transport (/mcp)"]
        Security["Origin Validator & Bearer Auth Middleware"]
        Router["MCPServer Tool & Resource Router"]

        subgraph Tools ["10 CareCircle MCP Tools"]
            T1["log_dose"]
            T2["whats_due"]
            T3["add_medication"]
            T4["add_note"]
            T5["add_appointment"]
            T6["care_summary"]
            T7["check_supply"]
            T8["request_refill (Confirm Gate)"]
            T9["prepare_visit_brief"]
            T10["forget (Confirm Gate)"]
        end

        subgraph Resources ["MCP Apps Resources"]
            R1["ui://carecircle/brief (text/html;profile=mcp-app)"]
        end

        subgraph Storage ["SQLite Database"]
            DB[(carecircle.db)]
            PLOG[(privacy_log)]
        end
    end

    UI -->|"Voice Utterance"| Agent
    Agent -->|"Streamable HTTP POST /mcp (Bearer Token)"| StreamHTTP
    StreamHTTP --> Security
    Security --> Router
    Router --> Tools
    Router --> Resources
    Tools --> DB
    T10 --> PLOG
    Resources -->|"ui:// HTML Template"| Host
    Host -->|"postMessage tools/call (Refill)"| Agent
```

---

## 5-Step Demo Story

The system supports the following real-world scenario end to end:

| Step | Speaker | Role | Spoken Voice Utterance | Alexa+ Spoken Reply & Action |
|:---|:---|:---|:---|:---|
| **1** | **Amma** | Patient (Morning) | *"I took my BP tablet."* | *"I've logged your Amlodipine (BP tablet) dose at 8:10 AM."* — Dose logged, pill count decremented. |
| **2** | **Ravi** | Caregiver (Evening) | *"Did Amma take her medicines today?"* | *"She took the BP tablet at 8:10 AM and Vitamin D3 at 9:00 AM. The 2:00 PM Metformin (sugar tablet) is not logged yet."* |
| **3** | **Priya** | Caregiver | *"Prepare me for Amma's cardiology visit on Monday."* | Synthesizes 14 days of doses, missed doses, and symptoms into a spoken summary and renders the **MCP Apps Doctor Brief Card** in the iframe. |
| **4** | **Ravi** | Caregiver | *"Her BP tablets are running low."* | *"Amlodipine (BP tablet) is running low. Should I place a mock pharmacy refill order?"* — **Requires explicit confirmation** before placing order `#MOCK-1`. |
| **5** | **Ravi** | Caregiver | *"Forget the note about her dizziness."* | Prompts for confirmation; upon confirmation, permanently erases note from database and writes an entry in `privacy_log`. |

---

## Demo Family & Roles

The seed script creates a realistic multi-generational family:

| Name | Role | Access Permissions |
|:---|:---|:---|
| **Amma** | `patient` | Read/Write (log doses, view schedule) |
| **Ravi** | `caregiver` | Read/Write (full care coordination & refills) |
| **Priya** | `caregiver` | Read/Write (doctor briefs, appointment notes) |
| **Dr. Rao's Clinic** | `viewer` | Read-Only (care summaries & doctor briefs) |

*(Note: Run `python scripts/seed.py` at any time to generate fresh tokens stored in `scripts/.tokens.json`)*.

---

## Quickstart & Installation

### Prerequisites
- Python 3.13
- Git

### 1. Setup Virtual Environment & Dependencies
```bash
# Clone the repository
git clone https://github.com/NanduKondreddy/CareCircle.git
cd carecircle

# Create and activate virtual environment
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Configure the LLM
Copy `.env.example` to `.env` and set your credentials:
```bash
cp .env.example .env
```
In `.env`, configure:
- `LLM_API_KEY`: Your Gemini or OpenAI-compatible API key.
- `LLM_BASE_URL`: `https://generativelanguage.googleapis.com/v1beta/openai` (or compatible endpoint).
- `LLM_MODEL`: `gemini-3.1-flash-lite` (or chosen model).

### 3. Populate Demo Data
```bash
python scripts/seed.py
```
This populates 14 days of realistic dose logs, 3 medications (BP tablet, sugar tablet, vitamin D), symptom notes (including morning dizziness), and an upcoming cardiology appointment.

### 4. Start the CareCircle MCP Server
```bash
python server/main.py
```
*Server starts on `http://127.0.0.1:8000/mcp`.*

### 5. Start the Simulated Alexa+ Voice Client
In a second terminal:
```bash
python client/app.py
```
*Voice client interface opens at `http://127.0.0.1:8001`.*

---

## Testing & Verification

### Automated Verification Suites
CareCircle includes 7 automated verification suites testing every layer of the MCP specification:

```bash
# Check 2: MCP Spec 2025-11-25 Protocol Version Handshake
python verify_init.py

# Live MCP Elicitation over Streamable HTTP (Modal Confirmation Flow)
python tests/test_real_elicitation.py

# Check 5: Unscripted Free Speech Test ("Has Amma missed any medicines this week?")
python tests/test_free_speech.py

# Phase 2: Origin check (403), bearer auth (401), role gates, and tools 1-6
python tests/test_phase2.py

# Phase 3: check_supply, request_refill, prepare_visit_brief, forget, privacy log
python tests/test_phase3.py

# Phase 4: MCP Apps card, ui:// resource discovery, MIME types, _meta linkage
python tests/test_phase4.py

# Phase 5: Complete 5-step demo story end-to-end
python tests/test_demo_story.py
```

### Testing with MCP Inspector
You can inspect and call all 10 tools directly using the official MCP Inspector:
```bash
npx @modelcontextprotocol/inspector
```
1. Set Transport to **Streamable HTTP**.
2. URL: `http://127.0.0.1:8000/mcp`.
3. Add Custom Headers:
   - `Authorization`: `Bearer <Ravi's token from scripts/.tokens.json>`
   - `Origin`: `http://localhost:8000`
4. Click **Connect**.
5. Test Elicitation: Call `request_refill` for `BP tablet`. You will see an elicitation confirmation modal appear in the Inspector!

### Testing in MCP Apps Hosts (MCPJam / Claude Desktop)
To test in an MCP Apps host such as [MCPJam](https://www.mcpjam.com) or Claude Desktop:
1. Ensure `server/main.py` is running on port 8000.
2. In host server settings, configure:
   ```json
   {
     "mcpServers": {
       "carecircle": {
         "url": "http://127.0.0.1:8000/mcp",
         "headers": {
           "Authorization": "Bearer <Ravi's token from scripts/.tokens.json>",
           "Origin": "http://localhost:8000"
         }
       }
     }
   }
   ```
3. Call `prepare_visit_brief` or `care_summary`. The host detects `_meta.ui.resourceUri = "ui://carecircle/brief"`, fetches the card via `resources/read`, and renders the visual interface inline with the interactive "Request Refill" button!

> [!NOTE]
> Our web client does not implement the separate-origin sandbox proxy that the spec requires for web hosts. Use MCPJam or Claude Desktop for a fully compliant host.

---

## Repository Structure

```
carecircle/
  server/
    main.py           # MCPServer entry point with Streamable HTTP & security middleware
    db.py             # Plain SQLite3 database connection and fuzzy medication search
    security.py       # SHA-256 token hashing, bearer auth, and origin validation
    ui/
      brief.html      # Self-contained MCP Apps card (ui://carecircle/brief)
    tools/
      phase2.py       # Tools 1-6 (log_dose, whats_due, add_medication, etc.)
      phase3.py       # Tools 7-10 (check_supply, refill, brief, forget)
  client/
    app.py            # Simulated Alexa+ voice client backend with agent router
    static/
      index.html      # Voice interface (STT, TTS, speaker switcher, iframe card)
  scripts/
    seed.py           # Database seeder creating demo family, meds, and logs
  tests/
    test_phase2.py    # Tests for auth, origin, and first 6 tools
    test_phase3.py    # Tests for confirmation gates, refills, briefs, and privacy log
    test_phase4.py    # Tests for MCP Apps UI card and resource discovery
    test_demo_story.py# End-to-end verification of the 5-step demo story
  SKILL.md            # Agent instructions for voice conversational style & safety
  FRICTION_LOG.md     # Engineering hurdles and workarounds encountered
  LICENSE             # MIT License
  requirements.txt    # Project dependencies
  .env.example        # Environment variable template
```

---

## Safety & Disclaimers

> [!IMPORTANT]
> **Mock Pharmacy Orders**: All pharmacy refills placed by `request_refill` are simulated **mock orders** (`status='placed (mock)'`). No real financial transactions, pharmacy APIs, or medication deliveries are conducted.

> [!NOTE]
> **Privacy Auditability**: Any deletion requested through `forget` is permanently recorded in `privacy_log` with actor ID, timestamp, and target entity for family transparency.

> [!NOTE]
> **MCP Apps Web Host Compliance**: Our web client does not implement the separate-origin sandbox proxy that the spec requires for web hosts. Use MCPJam or Claude Desktop for a fully compliant host.
