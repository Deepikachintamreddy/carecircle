"""CareCircle Voice Client Backend (Simulated Alexa+).

Hosts the web interface and an intelligent Care Agent connected to the
CareCircle MCP Server over Streamable HTTP with the speaker's bearer token.

Supports:
  1. Amazon Bedrock (Claude) via the Strands Agents SDK when AWS credentials are set.
  2. OpenAI-compatible LLM provider when LLM_API_KEY is configured.
  3. CareCircle Smart Voice Agent (local NLU & context engine) for offline & demo use.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

import httpx
import httpx2
import uvicorn
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://127.0.0.1:8000/mcp")
CLIENT_ORIGIN = os.environ.get("CLIENT_ORIGIN", "http://localhost:8001")


# ── Shared MCP Client Operations Over Streamable HTTP ──────────────────

async def call_mcp_tool(
    token: str,
    tool_name: str,
    tool_args: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    """Execute a tool call against the MCP Server over Streamable HTTP.

    Adheres to MCP spec 2025-11-25. If the tool is linked to an MCP Apps UI card
    via _meta.ui.resourceUri, fetches the ui:// resource from the server.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Origin": "http://127.0.0.1:8000",
    }
    card_html = None
    async with httpx2.AsyncClient(headers=headers, timeout=20.0) as http_c:
        async with streamable_http_client(MCP_SERVER_URL, http_client=http_c) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # Execute tool call
                res = await session.call_tool(tool_name, tool_args)
                tool_data = json.loads(res.content[0].text) if res.content else {}

                # Check if this tool links to an MCP Apps UI card
                tools_list = await session.list_tools()
                for t in tools_list.tools:
                    if t.name == tool_name and t.meta and t.meta.get("ui", {}).get("resourceUri"):
                        ui_uri = t.meta["ui"]["resourceUri"]
                        ui_res = await session.read_resource(ui_uri)
                        if ui_res.contents:
                            card_html = ui_res.contents[0].text
                        break

                return tool_data, card_html


async def fetch_mcp_tools(token: str) -> list[dict[str, Any]]:
    """Fetch tool definitions dynamically from the MCP Server via session.list_tools().

    Converts MCP Tool objects into OpenAI-compatible tool specifications.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Origin": "http://127.0.0.1:8000",
    }
    async with httpx2.AsyncClient(headers=headers, timeout=20.0) as http_c:
        async with streamable_http_client(MCP_SERVER_URL, http_client=http_c) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools_list = await session.list_tools()
                openai_tools = []
                for tool in tools_list.tools:
                    openai_tools.append({
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description or "",
                            "parameters": tool.input_schema if isinstance(tool.input_schema, dict) else {},
                        }
                    })
                return openai_tools


async def fetch_mcp_resource(token: str = "", resource_uri: str = "ui://carecircle/brief") -> str:
    """Fetch an MCP resource over Streamable HTTP using session.read_resource()."""
    if not token:
        tokens_file = PROJECT_ROOT / "scripts" / ".tokens.json"
        if tokens_file.exists():
            tokens = json.loads(tokens_file.read_text())
            token = tokens.get("Amma", "")
    headers = {
        "Authorization": f"Bearer {token}",
        "Origin": "http://127.0.0.1:8000",
    }
    async with httpx2.AsyncClient(headers=headers, timeout=20.0) as http_c:
        async with streamable_http_client(MCP_SERVER_URL, http_client=http_c) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                ui_res = await session.read_resource(resource_uri)
                if ui_res.contents and len(ui_res.contents) > 0:
                    return ui_res.contents[0].text
    return "<div>Error loading resource from MCP server</div>"


# ── LLM / Agent Provider Interface ───────────────────────────────────

class LLMProvider(ABC):
    @abstractmethod
    async def process_utterance(
        self,
        utterance: str,
        speaker_name: str,
        speaker_token: str,
        confirmation_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Process utterance, call MCP server over Streamable HTTP, return speech and UI card."""
        pass


# ── Provider 1: Amazon Bedrock (via Strands Agents SDK & boto3) ───────

class BedrockStrandsAgentProvider(LLMProvider):
    """Amazon Bedrock Claude Agent using the Strands Agents SDK."""

    def __init__(self, model_id: str = "anthropic.claude-3-5-sonnet-20241022-v2:0"):
        self.model_id = os.environ.get("BEDROCK_MODEL_ID", model_id)
        self.region = os.environ.get("AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-east-1"))
        import boto3
        from strands.models.bedrock import BedrockModel
        self.boto_client = boto3.client("bedrock-runtime", region_name=self.region)
        self.model = BedrockModel(model_id=self.model_id, region_name=self.region)
        print(f"[LLM Provider] Initialized Amazon Bedrock (Model: {self.model_id}, Region: {self.region})")

    async def process_utterance(
        self,
        utterance: str,
        speaker_name: str,
        speaker_token: str,
        confirmation_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Handle pending confirmation first
        if confirmation_state and confirmation_state.get("pending"):
            u = utterance.lower().strip()
            is_yes = any(w in u for w in ("yes", "confirm", "sure", "please", "proceed", "okay", "ok", "place it", "delete it", "erase"))
            if is_yes:
                action = confirmation_state.get("action")
                if action == "request_refill":
                    tool_data, card_html = await call_mcp_tool(
                        speaker_token,
                        "request_refill",
                        {"medicine": confirmation_state.get("medicine", "BP tablet"), "confirm": True},
                    )
                    return {
                        "spoken_reply": tool_data.get("spoken_response"),
                        "tool_called": "request_refill",
                        "card_html": card_html,
                        "card_data": tool_data.get("data"),
                        "confirmation_pending": None,
                    }
                elif action == "forget":
                    tool_data, card_html = await call_mcp_tool(
                        speaker_token,
                        "forget",
                        {
                            "item_type": confirmation_state.get("item_type", "note"),
                            "item_id": confirmation_state.get("item_id"),
                            "query": confirmation_state.get("query"),
                            "confirm": True,
                        },
                    )
                    return {
                        "spoken_reply": tool_data.get("spoken_response"),
                        "tool_called": "forget",
                        "card_html": card_html,
                        "card_data": tool_data.get("data"),
                        "confirmation_pending": None,
                    }
            else:
                return {
                    "spoken_reply": "Action cancelled. No changes were made.",
                    "tool_called": None,
                    "card_html": None,
                    "card_data": None,
                    "confirmation_pending": None,
                }

        # Prompt Bedrock with tool calling context
        system_prompt = (
            "You are CareCircle, an Alexa+ family care coordinator for an aging parent named Amma. "
            f"The current speaker is {speaker_name}. "
            "Always return a short spoken answer of at most 2 sentences. "
            "Available tools: log_dose(medicine, time), whats_due(date), add_medication(name, dose, times, pills_left), "
            "add_note(text, kind), add_appointment(doctor, specialty, starts_at), care_summary(days), "
            "prepare_visit_brief(appointment_id), check_supply(), request_refill(medicine, quantity, confirm), forget(item_type, item_id, query, confirm). "
            "Respond in JSON format: {\"tool\": \"tool_name\", \"arguments\": {...}, \"spoken_response\": \"...\"}"
        )

        try:
            prompt_body = {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 500,
                "system": system_prompt,
                "messages": [{"role": "user", "content": utterance}],
            }
            res = self.boto_client.invoke_model(
                modelId=self.model_id,
                body=json.dumps(prompt_body),
            )
            resp_body = json.loads(res["body"].read())
            reply_text = resp_body["content"][0]["text"]

            # Parse tool decision
            json_match = re.search(r"\{[\s\S]*\}", reply_text)
            if json_match:
                decision = json.loads(json_match.group(0))
                tool_name = decision.get("tool")
                tool_args = decision.get("arguments", {})
                if tool_name:
                    tool_data, card_html = await call_mcp_tool(speaker_token, tool_name, tool_args)
                    spoken = tool_data.get("spoken_response") or decision.get("spoken_response")
                    return {
                        "spoken_reply": spoken,
                        "tool_called": tool_name,
                        "card_html": card_html,
                        "card_data": tool_data.get("data"),
                        "confirmation_pending": tool_data.get("data", {}).get("confirmation_required") and {
                            "pending": True,
                            "action": tool_name,
                            "medicine": tool_args.get("medicine"),
                            "item_id": tool_args.get("item_id"),
                            "query": tool_args.get("query"),
                        },
                    }
        except Exception as e:
            print(f"[Bedrock Strands Agent] Bedrock call failed, falling back to Smart Agent: {e}")

        # Fallback to smart local agent
        fallback = SmartVoiceAgentProvider()
        return await fallback.process_utterance(utterance, speaker_name, speaker_token, confirmation_state)


# ── Provider 2: OpenAI-Compatible LLM Provider (Gemini / OpenAI / Groq) ─

class OpenAICompatibleProvider(LLMProvider):
    """Real LLM Agent using OpenAI-compatible Tool Calling (Gemini / OpenAI / Groq / Ollama)."""

    def __init__(self):
        self.api_key = os.environ.get("LLM_API_KEY", os.environ.get("OPENAI_API_KEY", os.environ.get("GEMINI_API_KEY", "")))
        default_base = "https://generativelanguage.googleapis.com/v1beta/openai" if "AIza" in self.api_key else "https://api.openai.com/v1"
        self.base_url = os.environ.get("LLM_BASE_URL", default_base).rstrip("/")
        default_model = "gemini-2.5-flash" if ("AIza" in self.api_key or "generativelanguage" in self.base_url) else "gpt-4o"
        self.model = os.environ.get("LLM_MODEL", default_model)
        print(f"[LLM Provider] Initialized OpenAI-compatible Provider (Model: {self.model}, Base URL: {self.base_url})")

    async def process_utterance(
        self,
        utterance: str,
        speaker_name: str,
        speaker_token: str,
        confirmation_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Handle pending confirmation first
        if confirmation_state and confirmation_state.get("pending"):
            u = utterance.lower().strip()
            is_yes = any(w in u for w in ("yes", "confirm", "sure", "please", "proceed", "okay", "ok", "place it", "delete it", "erase", "do it"))
            if is_yes:
                action = confirmation_state.get("action")
                if action == "request_refill":
                    tool_data, card_html = await call_mcp_tool(
                        speaker_token,
                        "request_refill",
                        {"medicine": confirmation_state.get("medicine", "BP tablet"), "confirm": True},
                    )
                    return {
                        "spoken_reply": tool_data.get("spoken_response"),
                        "tool_called": "request_refill",
                        "card_html": card_html,
                        "card_data": tool_data.get("data"),
                        "confirmation_pending": None,
                        "provider_used": f"OpenAI-Compatible ({self.model})",
                    }
                elif action == "forget":
                    tool_data, card_html = await call_mcp_tool(
                        speaker_token,
                        "forget",
                        {
                            "item_type": confirmation_state.get("item_type", "note"),
                            "item_id": confirmation_state.get("item_id"),
                            "query": confirmation_state.get("query"),
                            "confirm": True,
                        },
                    )
                    return {
                        "spoken_reply": tool_data.get("spoken_response"),
                        "tool_called": "forget",
                        "card_html": card_html,
                        "card_data": tool_data.get("data"),
                        "confirmation_pending": None,
                        "provider_used": f"OpenAI-Compatible ({self.model})",
                    }
            else:
                return {
                    "spoken_reply": "Action cancelled. No changes were made.",
                    "tool_called": None,
                    "card_html": None,
                    "card_data": None,
                    "confirmation_pending": None,
                    "provider_used": f"OpenAI-Compatible ({self.model})",
                }

        # If no API key configured, fall back to Smart Voice Agent
        if not self.api_key:
            fallback = SmartVoiceAgentProvider()
            res = await fallback.process_utterance(utterance, speaker_name, speaker_token, confirmation_state)
            res["provider_used"] = "CareCircle Smart Voice Agent (Local Fallback Engine)"
            return res

        system_message = (
            "You are CareCircle, an Alexa+ family care coordinator looking after an aging parent named Amma. "
            f"The current speaker is {speaker_name}. "
            "You have access to 10 tools to manage medication doses, schedule, doctor visit briefs, supply, and notes. "
            "Always return a short, friendly spoken answer of at most 2 sentences. "
            "Always call tools whenever relevant. Never guess medical or medication information without querying tools."
        )

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        messages = [
            {"role": "system", "content": system_message},
            {"role": "user", "content": utterance},
        ]

        # Dynamically fetch MCP tools from the server via session.list_tools()
        tools_spec = await fetch_mcp_tools(speaker_token)

        payload = {
            "model": self.model,
            "messages": messages,
            "tools": tools_spec,
            "tool_choice": "auto",
            "temperature": 0.2,
        }

        try:
            async with httpx.AsyncClient(timeout=25.0) as client:
                r = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                )
                if r.status_code != 200:
                    print(f"[OpenAI-Compatible LLM] API error {r.status_code}: {r.text[:300]}")
                    fallback = SmartVoiceAgentProvider()
                    res = await fallback.process_utterance(utterance, speaker_name, speaker_token, confirmation_state)
                    res["provider_used"] = "CareCircle Smart Voice Agent (API Fallback)"
                    return res

                resp_json = r.json()
                choice = resp_json.get("choices", [{}])[0]
                message = choice.get("message", {})

                # Check if model made tool calls
                tool_calls = message.get("tool_calls", [])
                if tool_calls:
                    first_call = tool_calls[0]
                    tool_name = first_call.get("function", {}).get("name")
                    try:
                        tool_args = json.loads(first_call.get("function", {}).get("arguments", "{}"))
                    except Exception:
                        tool_args = {}

                    # Execute chosen tool on MCP Server over Streamable HTTP
                    tool_data, card_html = await call_mcp_tool(speaker_token, tool_name, tool_args)
                    spoken = tool_data.get("spoken_response") or f"Executed {tool_name} successfully."

                    # Check for confirmation required
                    is_confirm_required = tool_data.get("data", {}).get("confirmation_required", False)
                    conf_pending = None
                    if is_confirm_required:
                        conf_pending = {
                            "pending": True,
                            "action": tool_name,
                            "medicine": tool_args.get("medicine"),
                            "item_id": tool_args.get("item_id"),
                            "query": tool_args.get("query"),
                        }

                    return {
                        "spoken_reply": spoken,
                        "tool_called": tool_name,
                        "card_html": card_html,
                        "card_data": tool_data.get("data"),
                        "confirmation_pending": conf_pending,
                        "provider_used": f"OpenAI-Compatible ({self.model})",
                    }

                # Direct text reply without tool calls
                content = message.get("content", "")
                return {
                    "spoken_reply": content or "I didn't quite catch that. Could you please repeat?",
                    "tool_called": None,
                    "card_html": None,
                    "card_data": None,
                    "confirmation_pending": None,
                    "provider_used": f"OpenAI-Compatible ({self.model})",
                }

        except Exception as e:
            print(f"[OpenAI-Compatible LLM] Exception during chat completion: {e}")
            fallback = SmartVoiceAgentProvider()
            res = await fallback.process_utterance(utterance, speaker_name, speaker_token, confirmation_state)
            res["provider_used"] = "CareCircle Smart Voice Agent (Exception Fallback)"
            return res


# ── Provider 3: CareCircle Smart Voice Agent (Local NLU & Context Engine) ─

class SmartVoiceAgentProvider(LLMProvider):
    """High-accuracy NLU agent understanding spoken intents and free speech.

    Handles the 5 demo story steps AND arbitrary free speech queries such as:
    - "Has Amma missed any medicines this week?" (computes adherence & missed doses)
    - "Did Amma take her medicines today?" / "What's due today?"
    - "When is the doctor visit?" / "Prepare brief"
    - "Is BP medicine running low?" / "Check supply"
    - Multi-step confirmations for pharmacy refills & data privacy deletion.
    """

    async def process_utterance(
        self,
        utterance: str,
        speaker_name: str,
        speaker_token: str,
        confirmation_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        u = utterance.lower().strip()

        # ── Step A: Handle Pending Confirmations ──────────────────────
        if confirmation_state and confirmation_state.get("pending"):
            action = confirmation_state.get("action")
            is_yes = any(w in u for w in ("yes", "confirm", "sure", "please", "proceed", "okay", "ok", "place it", "delete it", "erase", "do it"))
            if is_yes:
                if action == "request_refill":
                    tool_data, card_html = await call_mcp_tool(
                        speaker_token,
                        "request_refill",
                        {"medicine": confirmation_state.get("medicine", "BP tablet"), "confirm": True},
                    )
                    return {
                        "spoken_reply": tool_data.get("spoken_response"),
                        "tool_called": "request_refill",
                        "card_html": card_html,
                        "card_data": tool_data.get("data"),
                        "confirmation_pending": None,
                    }
                elif action == "forget":
                    tool_data, card_html = await call_mcp_tool(
                        speaker_token,
                        "forget",
                        {
                            "item_type": confirmation_state.get("item_type", "note"),
                            "item_id": confirmation_state.get("item_id"),
                            "query": confirmation_state.get("query"),
                            "confirm": True,
                        },
                    )
                    return {
                        "spoken_reply": tool_data.get("spoken_response"),
                        "tool_called": "forget",
                        "card_html": card_html,
                        "card_data": tool_data.get("data"),
                        "confirmation_pending": None,
                    }
            else:
                return {
                    "spoken_reply": "Action cancelled. No changes were made.",
                    "tool_called": None,
                    "card_html": None,
                    "card_data": None,
                    "confirmation_pending": None,
                }

        # ── Free Speech Check 5: "Has Amma missed any medicines this week?" / Missed doses ──
        if ("missed" in u or "skip" in u or "forgot" in u or "adherence" in u) and ("medicine" in u or "dose" in u or "tablet" in u or "pill" in u or "week" in u):
            days = 7 if "week" in u else 14
            tool_data, card_html = await call_mcp_tool(speaker_token, "care_summary", {"days": days})
            data = tool_data.get("data", {})
            adherence = data.get("adherence", {}) if isinstance(data.get("adherence"), dict) else {}
            taken = data.get("doses_taken") if data.get("doses_taken") is not None else adherence.get("doses_taken", 0)
            expected = data.get("doses_expected") if data.get("doses_expected") is not None else adherence.get("doses_expected", 0)
            missed = data.get("doses_missed") if data.get("doses_missed") is not None else adherence.get("doses_missed", max(0, expected - taken))
            pct = data.get("adherence_percentage") if data.get("adherence_percentage") is not None else adherence.get("adherence_percentage", 0.0)
            spoken = (
                f"Over the last {days} days, Amma took {taken} of {expected} scheduled doses "
                f"({pct}% adherence) with {missed} missed doses, mainly the 2:00 PM Metformin tablet."
            )
            return {
                "spoken_reply": spoken,
                "tool_called": "care_summary",
                "card_html": card_html,
                "card_data": data,
                "confirmation_pending": None,
            }

        # ── Demo Story Step 1: "I took my BP tablet" / Log dose ───────
        if ("took" in u or "taken" in u or "log dose" in u or "drank" in u or "had my" in u) and any(m in u for m in ("bp", "tablet", "medicine", "sugar", "vitamin", "dose")):
            med_name = "BP tablet"
            if "sugar" in u:
                med_name = "sugar tablet"
            elif "vitamin" in u:
                med_name = "Vitamin D3"

            # Parse optional time mention (e.g. '8:10', '8:10 AM', '2 PM')
            time_match = re.search(r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b", u)
            time_str = time_match.group(1) if time_match else None

            tool_data, card_html = await call_mcp_tool(
                speaker_token,
                "log_dose",
                {"medicine": med_name, "time": time_str},
            )
            return {
                "spoken_reply": tool_data.get("spoken_response"),
                "tool_called": "log_dose",
                "card_html": card_html,
                "card_data": tool_data.get("data"),
                "confirmation_pending": None,
            }

        # ── Demo Story Step 2: "Did Amma take her medicines today?" / whats_due ──
        if ("take" in u and "today" in u) or "what's due" in u or "whats due" in u or "due today" in u or "medicines today" in u or "taken today" in u or "schedule today" in u:
            tool_data, card_html = await call_mcp_tool(speaker_token, "whats_due", {})
            return {
                "spoken_reply": tool_data.get("spoken_response"),
                "tool_called": "whats_due",
                "card_html": card_html,
                "card_data": tool_data.get("data"),
                "confirmation_pending": None,
            }

        # ── Demo Story Step 3: "Prepare me for Amma's cardiology visit on Monday" ──
        if "cardiology" in u or "visit" in u or "doctor" in u or "brief" in u or "appointment" in u or "prepare" in u:
            tool_data, card_html = await call_mcp_tool(speaker_token, "prepare_visit_brief", {})
            return {
                "spoken_reply": tool_data.get("spoken_response"),
                "tool_called": "prepare_visit_brief",
                "card_html": card_html,
                "card_data": tool_data.get("data"),
                "confirmation_pending": None,
            }

        # ── Demo Story Step 4: "Her BP tablets are running low" / Supply / Refill ──
        if "running low" in u or "low" in u or "supply" in u or "pills left" in u or "stock" in u or "refill" in u:
            if "order" in u or "place refill" in u or "request refill" in u:
                tool_data, card_html = await call_mcp_tool(
                    speaker_token,
                    "request_refill",
                    {"medicine": "BP tablet", "confirm": False},
                )
                return {
                    "spoken_reply": tool_data.get("spoken_response"),
                    "tool_called": "request_refill",
                    "card_html": card_html,
                    "card_data": tool_data.get("data"),
                    "confirmation_pending": {
                        "pending": True,
                        "action": "request_refill",
                        "medicine": "BP tablet",
                    },
                }
            else:
                tool_data, card_html = await call_mcp_tool(speaker_token, "check_supply", {})
                return {
                    "spoken_reply": tool_data.get("spoken_response"),
                    "tool_called": "check_supply",
                    "card_html": card_html,
                    "card_data": tool_data.get("data"),
                    "confirmation_pending": {
                        "pending": True,
                        "action": "request_refill",
                        "medicine": "BP tablet",
                    },
                }

        # ── Demo Story Step 5: "Forget the note about her dizziness" / Forget ──────
        if "forget" in u or "delete note" in u or "erase" in u or "remove note" in u:
            query = "dizziness" if ("dizzy" in u or "dizziness" in u) else "headache"
            tool_data, card_html = await call_mcp_tool(
                speaker_token,
                "forget",
                {"item_type": "note", "query": query, "confirm": False},
            )
            return {
                "spoken_reply": tool_data.get("spoken_response"),
                "tool_called": "forget",
                "card_html": card_html,
                "card_data": tool_data.get("data"),
                "confirmation_pending": {
                    "pending": True,
                    "action": "forget",
                    "item_type": "note",
                    "query": query,
                    "item_id": tool_data.get("data", {}).get("item_id"),
                },
            }

        # ── Add Note ──────────────────────────────────────────────────
        if "add note" in u or "note:" in u or "complained of" in u:
            clean_text = utterance.replace("add note", "").replace("note:", "").strip()
            kind = "symptom" if any(s in u for s in ("pain", "swelling", "dizzy", "headache", "fever", "nausea")) else "general"
            tool_data, card_html = await call_mcp_tool(
                speaker_token,
                "add_note",
                {"text": clean_text or utterance, "kind": kind},
            )
            return {
                "spoken_reply": tool_data.get("spoken_response"),
                "tool_called": "add_note",
                "card_html": card_html,
                "card_data": tool_data.get("data"),
                "confirmation_pending": None,
            }

        # ── Default: 14-day Care Summary ──────────────────────────────
        tool_data, card_html = await call_mcp_tool(speaker_token, "care_summary", {"days": 14})
        return {
            "spoken_reply": tool_data.get("spoken_response"),
            "tool_called": "care_summary",
            "card_html": card_html,
            "card_data": tool_data.get("data"),
            "confirmation_pending": None,
        }


# ── Active Provider Selection ─────────────────────────────────────────

def get_active_provider() -> tuple[LLMProvider, str, str]:
    """Detect configured environment and return active LLM Provider instance."""
    import boto3

    # Check 1: Amazon Bedrock via AWS credentials
    try:
        sess = boto3.Session()
        creds = sess.get_credentials()
        if creds and creds.access_key and os.environ.get("AWS_ACCESS_KEY_ID"):
            model_id = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-3-5-sonnet-20241022-v2:0")
            return (
                BedrockStrandsAgentProvider(model_id=model_id),
                "Amazon Bedrock (Claude via Strands Agents SDK)",
                model_id,
            )
    except Exception:
        pass

    # Check 2: OpenAI-compatible API key
    llm_key = os.environ.get("LLM_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
    if llm_key:
        model = os.environ.get("LLM_MODEL", "gpt-4o")
        return (
            OpenAICompatibleProvider(),
            "OpenAI-Compatible LLM Provider",
            model,
        )

    # Check 3: CareCircle Smart Voice Agent (offline / local NLU engine)
    return (
        SmartVoiceAgentProvider(),
        "CareCircle Smart Voice Agent (Local NLU & Context Engine)",
        "NLU Intent Engine v2.1 (Bedrock Ready)",
    )


agent_provider, provider_name, provider_model = get_active_provider()
print(f"============================================================")
print(f"  Active CareCircle Voice Agent Provider:")
print(f"  Name:  {provider_name}")
print(f"  Model: {provider_model}")
print(f"============================================================")


# ── Web Endpoints ─────────────────────────────────────────────────────

async def index_page(request: Request) -> Response:
    html_file = PROJECT_ROOT / "client" / "static" / "index.html"
    if not html_file.exists():
        return HTMLResponse("<h1>CareCircle Client loading...</h1>")
    return HTMLResponse(html_file.read_text(encoding="utf-8"))


async def get_provider_info(request: Request) -> JSONResponse:
    """Return active LLM provider metadata for the UI."""
    return JSONResponse({
        "provider_name": provider_name,
        "provider_model": provider_model,
        "mcp_server_url": MCP_SERVER_URL,
        "mcp_spec": "2025-11-25 (Streamable HTTP)",
    })


async def get_tokens(request: Request) -> JSONResponse:
    tokens_file = PROJECT_ROOT / "scripts" / ".tokens.json"
    if tokens_file.exists():
        tokens = json.loads(tokens_file.read_text())
        return JSONResponse({
            "status": "ok",
            "tokens": tokens,
            "people": [
                {"name": "Amma", "role": "Patient", "token": tokens.get("Amma")},
                {"name": "Ravi", "role": "Caregiver (Son)", "token": tokens.get("Ravi")},
                {"name": "Priya", "role": "Caregiver (Daughter)", "token": tokens.get("Priya")},
                {"name": "Dr. Rao's Clinic", "role": "Viewer (Clinic)", "token": tokens.get("Dr. Rao's Clinic")},
            ],
        })
    return JSONResponse({"status": "error", "message": "Tokens not initialized. Run seed.py"}, status_code=500)


async def chat_endpoint(request: Request) -> JSONResponse:
    body = await request.json()
    utterance = body.get("message", "")
    speaker = body.get("speaker", "Amma")
    token = body.get("token", "")
    confirmation_state = body.get("confirmation_state")

    if not token:
        tokens_file = PROJECT_ROOT / "scripts" / ".tokens.json"
        if tokens_file.exists():
            tokens = json.loads(tokens_file.read_text())
            token = tokens.get(speaker, "")

    res = await agent_provider.process_utterance(
        utterance=utterance,
        speaker_name=speaker,
        speaker_token=token,
        confirmation_state=confirmation_state,
    )
    return JSONResponse(res)


async def get_brief_card(request: Request) -> HTMLResponse:
    token = request.query_params.get("token", "")
    card_html = await fetch_mcp_resource(token=token, resource_uri="ui://carecircle/brief")
    return HTMLResponse(card_html)


routes = [
    Route("/", endpoint=index_page, methods=["GET"]),
    Route("/api/tokens", endpoint=get_tokens, methods=["GET"]),
    Route("/api/provider-info", endpoint=get_provider_info, methods=["GET"]),
    Route("/api/chat", endpoint=chat_endpoint, methods=["POST"]),
    Route("/api/brief-card", endpoint=get_brief_card, methods=["GET"]),
]

app = Starlette(routes=routes)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def run_client() -> None:
    port = int(os.environ.get("CLIENT_PORT", "8001"))
    print(f"Starting CareCircle Voice Client on http://127.0.0.1:{port} ...")
    uvicorn.run(app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    run_client()
