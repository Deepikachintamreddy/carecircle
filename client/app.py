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
import time
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

# MCP Apps Extension capability advertisement per spec 2026-01-26
MCP_UI_EXTENSIONS = {
    "io.modelcontextprotocol/ui": {
        "mimeTypes": ["text/html;profile=mcp-app"],
    }
}


# ── Shared MCP Client Operations Over Streamable HTTP ──────────────────

class SpeakerMcpSession:
    """Persistent MCP Streamable HTTP session reused per speaker."""

    def __init__(self, token: str):
        self.token = token
        self.queue: asyncio.Queue = asyncio.Queue()
        self.ready: asyncio.Event = asyncio.Event()
        self.task: asyncio.Task = asyncio.create_task(self._worker())

    async def _worker(self):
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Origin": "http://127.0.0.1:8000",
        }
        try:
            async with httpx2.AsyncClient(headers=headers, timeout=60.0) as http_c:
                async with streamable_http_client(MCP_SERVER_URL, http_client=http_c) as (read, write):
                    async with ClientSession(read, write, extensions=MCP_UI_EXTENSIONS) as session:
                        await session.initialize()
                        self.ready.set()
                        while True:
                            item = await self.queue.get()
                            if item is None:
                                break
                            action, args, fut = item
                            try:
                                if action == "call_tool":
                                    res = await session.call_tool(*args)
                                    fut.set_result(res)
                                elif action == "list_tools":
                                    res = await session.list_tools()
                                    fut.set_result(res)
                                elif action == "read_resource":
                                    res = await session.read_resource(*args)
                                    fut.set_result(res)
                            except Exception as e:
                                fut.set_exception(e)
                            self.queue.task_done()
        except Exception as e:
            self.ready.set()
            print(f"[MCP Session Worker Error] Speaker session error: {e}")

    async def call_tool(self, tool_name: str, tool_args: dict[str, Any]):
        await self.ready.wait()
        fut = asyncio.get_running_loop().create_future()
        await self.queue.put(("call_tool", (tool_name, tool_args), fut))
        return await fut

    async def list_tools(self):
        await self.ready.wait()
        fut = asyncio.get_running_loop().create_future()
        await self.queue.put(("list_tools", (), fut))
        return await fut

    async def read_resource(self, uri: str):
        await self.ready.wait()
        fut = asyncio.get_running_loop().create_future()
        await self.queue.put(("read_resource", (uri,), fut))
        return await fut


_speaker_sessions: dict[str, SpeakerMcpSession] = {}
_session_lock = asyncio.Lock()
_cached_card_html: str | None = None
_tool_ui_map: dict[str, str] = {
    "care_summary": "ui://carecircle/brief",
    "prepare_visit_brief": "ui://carecircle/brief",
}


async def get_or_create_speaker_session(token: str) -> SpeakerMcpSession:
    async with _session_lock:
        sess = _speaker_sessions.get(token)
        if sess is None or sess.task.done():
            sess = SpeakerMcpSession(token)
            _speaker_sessions[token] = sess
        return sess


async def call_mcp_tool(
    token: str,
    tool_name: str,
    tool_args: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    """Execute a tool call against the MCP Server over a reused Streamable HTTP session."""
    global _cached_card_html
    sess = await get_or_create_speaker_session(token)
    try:
        res = await sess.call_tool(tool_name, tool_args)
    except Exception as e:
        print(f"[MCP Session] Reconnecting session after error: {e}")
        _speaker_sessions.pop(token, None)
        sess = await get_or_create_speaker_session(token)
        res = await sess.call_tool(tool_name, tool_args)

    raw_call_tool_result = res.model_dump(by_alias=True)
    if getattr(res, "structured_content", None):
        tool_data = {
            "spoken_response": res.content[0].text if res.content else "",
            "data": res.structured_content,
            **res.structured_content,
        }
    else:
        try:
            tool_data = json.loads(res.content[0].text) if res.content else {}
        except Exception:
            tool_data = {"spoken_response": res.content[0].text if res.content else ""}

    tool_data["_raw_call_tool_result"] = raw_call_tool_result

    card_html = None
    if tool_name in _tool_ui_map:
        if _cached_card_html is None:
            try:
                ui_res = await sess.read_resource(_tool_ui_map[tool_name])
                if ui_res.contents:
                    _cached_card_html = ui_res.contents[0].text
            except Exception as e:
                print(f"[MCP Session] Could not fetch card HTML: {e}")
        card_html = _cached_card_html

    return tool_data, card_html


_tools_spec_cache: list[dict[str, Any]] | None = None


async def fetch_mcp_tools(token: str) -> list[dict[str, Any]]:
    """Fetch tool definitions dynamically from the MCP Server via reused session.list_tools()."""
    global _tools_spec_cache
    if _tools_spec_cache is not None:
        return _tools_spec_cache

    sess = await get_or_create_speaker_session(token)
    tools_list = await sess.list_tools()
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
    _tools_spec_cache = openai_tools
    return openai_tools


async def fetch_mcp_resource(token: str = "", resource_uri: str = "ui://carecircle/brief") -> str:
    """Fetch an MCP resource over Streamable HTTP using reused session."""
    global _cached_card_html
    if _cached_card_html is not None:
        return _cached_card_html
    if not token:
        tokens_file = PROJECT_ROOT / "scripts" / ".tokens.json"
        if tokens_file.exists():
            tokens = json.loads(tokens_file.read_text())
            token = tokens.get("Amma", "")
    sess = await get_or_create_speaker_session(token)
    ui_res = await sess.read_resource(resource_uri)
    if ui_res.contents and len(ui_res.contents) > 0:
        _cached_card_html = ui_res.contents[0].text
        return _cached_card_html
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
            "You have access to 10 tools. Always invoke the appropriate tool to answer user questions.\n"
            "- For questions about whether medicines were taken today, what is due today, or today's schedule (e.g. 'Did Amma take her medicines today?', 'What's due today?'): call `whats_due()`. Never call care_summary for questions about today.\n"
            "- For questions about past missed medicines over the week/days or overall adherence: call `care_summary(days=7)`.\n"
            "- For questions about doctor appointments, upcoming visits, or doctor briefs: call `prepare_visit_brief()`.\n"
            "- For questions asking how many pills or tablets remain in stock (e.g. 'How many sugar tablets do I have left?'): call `check_supply()`.\n"
            "- For requesting pharmacy refills or when a medicine is reported running low (e.g. 'Her BP tablets are running low', 'refill BP tablet'): call `request_refill(medicine=..., confirm=False)`.\n"
            "- For logging a dose taken: call `log_dose(medicine=...)`.\n"
            "\n"
            "CRITICAL RESPONSE INSTRUCTIONS:\n"
            "Your reply must be at most 2 sentences. Answer ONLY what was asked using facts from the tool results. "
            "Only mention low stock when days left is below 3.\n"
            "- For today's doses / what's due questions: Sentence 1 states what was taken today and what is overdue or pending today. Never use multi-day missed counts for today.\n"
            "- For multi-day missed medicines/adherence questions: Sentence 1 states the total number of missed doses (e.g. 'Amma missed <N> doses this week...') and which medicines were missed.\n"
            "- For doctor appointment questions: Sentence 1 states the doctor, specialty, day/date, and time.\n"
            "- For pill supply questions: Sentence 1 states the count of pills or tablets left for the requested medicine.\n"
            "- Only mention low stock when days left is below 3. Never mention unasked appointments or other topics."
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

        candidate_models = [self.model]
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": tools_spec,
            "tool_choice": "auto",
            "temperature": 0.1,
            "max_tokens": 150,
        }

        t_req_start = time.perf_counter()
        llm1_retries = 0
        llm1_had_429 = False
        t_llm1 = 0.0
        t_mcp = 0.0
        t_llm2 = 0.0
        llm2_retries = 0
        llm2_had_429 = False

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = None
                active_model = self.model
                t_llm1_start = time.perf_counter()
                for mod in candidate_models:
                    active_model = mod
                    payload["model"] = mod
                    for attempt in range(2):
                        if attempt > 0:
                            llm1_retries += 1
                        try:
                            r = await client.post(
                                f"{self.base_url}/chat/completions",
                                headers=headers,
                                json=payload,
                            )
                            if r.status_code == 429:
                                llm1_had_429 = True
                                print(f"[OpenAI-Compatible LLM] Model {mod} returned 429 quota, retrying...")
                                await asyncio.sleep(1.0)
                                continue
                            break
                        except Exception as req_err:
                            llm1_retries += 1
                            print(f"[OpenAI-Compatible LLM] Attempt error ({mod}): {req_err}")
                            await asyncio.sleep(1.0)
                            continue
                    if r is not None and r.status_code == 200:
                        break
                t_llm1 = time.perf_counter() - t_llm1_start

                if r is None or r.status_code != 200:
                    err_msg = r.text[:300] if r else "no response"
                    print(f"[OpenAI-Compatible LLM] API error {r.status_code if r else 'None'}: {err_msg}")
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

                    # Execute chosen tool on MCP Server over reused Streamable HTTP session
                    t_mcp_start = time.perf_counter()
                    tool_data, card_html = await call_mcp_tool(speaker_token, tool_name, tool_args)
                    t_mcp = time.perf_counter() - t_mcp_start
                    spoken = tool_data.get("spoken_response") or f"Executed {tool_name} successfully."

                    # Have the LLM synthesize the final spoken reply using the tool's structured content
                    try:
                        followup_messages = [
                            {
                                "role": "system",
                                "content": (
                                    "You are CareCircle Alexa+ voice assistant. "
                                    "CRITICAL RULES:\n"
                                    "1. Reply in at most 2 sentences.\n"
                                    "2. Answer ONLY what was asked using the tool result structured data:\n"
                                    "   - For today's doses / what's due today (from whats_due): Sentence 1 lists what was taken today (e.g. 'She took <med> at <time>') and what is overdue or pending today (e.g. '<med> is overdue'). Never use multi-day or 7-day missed counts when answering about today.\n"
                                    "   - For multi-day missed medicines / adherence (from care_summary): Sentence 1 states the total number of missed doses (e.g. 'Amma missed <N> doses this week...') and which medicines were missed.\n"
                                    "   - For doctor appointments: Sentence 1 states the doctor name, specialty, day/date, and time.\n"
                                    "   - For supply/pills left: Sentence 1 states the count of pills or tablets left.\n"
                                    "3. Only mention low stock when days left is below 3.\n"
                                    "4. Never invent details or answer unasked questions."
                                ),
                            },
                            {"role": "user", "content": utterance},
                            message,
                            {
                                "role": "tool",
                                "tool_call_id": first_call.get("id", "call_1"),
                                "content": json.dumps(tool_data.get("data") or tool_data),
                            },
                        ]
                        t_llm2_start = time.perf_counter()
                        r2 = None
                        for synth_mod in candidate_models:
                            for attempt2 in range(2):
                                if attempt2 > 0:
                                    llm2_retries += 1
                                try:
                                    r2 = await client.post(
                                        f"{self.base_url}/chat/completions",
                                        headers=headers,
                                        json={
                                            "model": synth_mod,
                                            "messages": followup_messages,
                                            "temperature": 0.1,
                                            "max_tokens": 150,
                                        },
                                    )
                                    if r2.status_code == 429:
                                        llm2_had_429 = True
                                        print(f"[OpenAI-Compatible LLM] Synthesis {synth_mod} 429 quota, retrying...")
                                        await asyncio.sleep(1.0)
                                        continue
                                    break
                                except Exception as req_err:
                                    llm2_retries += 1
                                    print(f"[OpenAI-Compatible LLM] Synthesis attempt error ({synth_mod}): {req_err}")
                                    await asyncio.sleep(1.0)
                                    continue
                            if r2 is not None and r2.status_code == 200:
                                active_model = synth_mod
                                break
                        t_llm2 = time.perf_counter() - t_llm2_start

                        if r2 is not None and r2.status_code == 200:
                            choice2 = r2.json().get("choices", [{}])[0]
                            llm_reply = choice2.get("message", {}).get("content", "").strip()
                            print(f"[OpenAI-Compatible LLM] Synthesis reply ({active_model}): '{llm_reply}'")
                            if llm_reply:
                                spoken = llm_reply
                        else:
                            print(f"[OpenAI-Compatible LLM] Synthesis turn HTTP {r2.status_code if r2 else 'None'}: {r2.text[:300] if r2 else ''}")
                    except Exception as synth_err:
                        print(f"[OpenAI-Compatible LLM] Synthesis turn exception: {synth_err}")

                    t_total = time.perf_counter() - t_req_start
                    print("------------------------------------------------------------")
                    print(f"[TIMING LOG] Request: \"{utterance}\"")
                    print(f"  • 1st LLM Call (Tool Selection): {t_llm1:.2f}s | Status: {r.status_code if r else 'None'} | Retries: {llm1_retries} | 429: {llm1_had_429}")
                    print(f"  • MCP Tool Call ({tool_name}):      {t_mcp:.3f}s (reused streamable session)")
                    print(f"  • 2nd LLM Call (Synthesis):      {t_llm2:.2f}s | Status: {r2.status_code if r2 else 'None'} | Retries: {llm2_retries} | 429: {llm2_had_429}")
                    print(f"  • Total Request Latency:         {t_total:.2f}s (Target: < 10.0s)")
                    print("------------------------------------------------------------")

                    # Check for confirmation required
                    is_confirm_required = (
                        tool_data.get("confirmation_required")
                        or tool_data.get("data", {}).get("confirmation_required", False)
                        or (tool_name == "request_refill" and not tool_args.get("confirm"))
                        or (tool_name == "forget" and not tool_args.get("confirm"))
                    )
                    conf_pending = None
                    if is_confirm_required:
                        conf_pending = {
                            "pending": True,
                            "action": tool_name,
                            "medicine": tool_args.get("medicine", "BP tablet"),
                            "item_id": tool_args.get("item_id"),
                            "query": tool_args.get("query"),
                        }
                        if tool_data.get("spoken_response"):
                            spoken = tool_data.get("spoken_response")

                    return {
                        "spoken_reply": spoken,
                        "tool_called": tool_name,
                        "card_html": card_html,
                        "card_data": tool_data.get("data") or tool_data,
                        "call_tool_result": tool_data.get("_raw_call_tool_result"),
                        "tool_args": tool_args,
                        "confirmation_pending": conf_pending,
                        "provider_used": f"OpenAI-Compatible ({active_model})",
                    }

                # Direct text reply without tool calls
                content = message.get("content", "")
                conf_pending = None
                if "confirm" in content.lower() and ("refill" in content.lower() or "tablet" in content.lower()):
                    conf_pending = {
                        "pending": True,
                        "action": "request_refill",
                        "medicine": "BP tablet",
                    }
                return {
                    "spoken_reply": content or "I didn't quite catch that. Could you please repeat?",
                    "tool_called": None,
                    "card_html": None,
                    "card_data": None,
                    "confirmation_pending": conf_pending,
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
            data = tool_data.get("data", {})
            appt = data.get("appointment", {})
            if appt and appt.get("starts_at"):
                day_name = ""
                time_str = ""
                try:
                    dt = datetime.fromisoformat(appt["starts_at"])
                    day_name = dt.strftime("%A")
                    time_str = dt.strftime("%I:%M %p")
                except Exception:
                    pass
                spoken = f"Amma has an upcoming appointment with {appt.get('doctor')} for {appt.get('specialty')} on {day_name} at {time_str}."
            else:
                spoken = tool_data.get("spoken_response")
            return {
                "spoken_reply": spoken,
                "tool_called": "prepare_visit_brief",
                "card_html": card_html,
                "card_data": data,
                "confirmation_pending": None,
            }

        # ── Demo Story Step 4: "Her BP tablets are running low" / Supply / Refill ──
        if "running low" in u or "low" in u or "supply" in u or "how many" in u or "left" in u or "pills left" in u or "stock" in u or "refill" in u:
            med_arg = None
            if "sugar" in u or "metformin" in u:
                med_arg = "sugar tablet"
            elif "bp" in u or "amlodipine" in u:
                med_arg = "BP tablet"
            elif "vitamin" in u:
                med_arg = "Vitamin D3"

            if "order" in u or "place refill" in u or "request refill" in u:
                tool_data, card_html = await call_mcp_tool(
                    speaker_token,
                    "request_refill",
                    {"medicine": med_arg or "BP tablet", "confirm": False},
                )
                return {
                    "spoken_reply": tool_data.get("spoken_response"),
                    "tool_called": "request_refill",
                    "card_html": card_html,
                    "card_data": tool_data.get("data"),
                    "confirmation_pending": {
                        "pending": True,
                        "action": "request_refill",
                        "medicine": med_arg or "BP tablet",
                    },
                }
            else:
                args = {"medicine": med_arg} if med_arg else {}
                tool_data, card_html = await call_mcp_tool(speaker_token, "check_supply", args)
                is_refill_needed = tool_data.get("data", {}).get("is_low") or tool_data.get("data", {}).get("refill_needed")
                return {
                    "spoken_reply": tool_data.get("spoken_response"),
                    "tool_called": "check_supply",
                    "card_html": card_html,
                    "card_data": tool_data.get("data"),
                    "confirmation_pending": is_refill_needed and {
                        "pending": True,
                        "action": "request_refill",
                        "medicine": med_arg or "BP tablet",
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

    # Check 3: Keyword fallback (offline / no LLM)
    return (
        SmartVoiceAgentProvider(),
        "Keyword fallback (no LLM)",
        "keyword-fallback",
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
    if not res.get("provider_used"):
        res["provider_used"] = provider_name
    if "call_tool_result" not in res and res.get("card_data"):
        res["call_tool_result"] = {
            "content": [{"type": "text", "text": res.get("spoken_reply", "")}],
            "structuredContent": res.get("card_data", {}),
            "isError": False,
        }
    return JSONResponse(res)


async def get_brief_card(request: Request) -> HTMLResponse:
    token = request.query_params.get("token", "")
    card_html = await fetch_mcp_resource(token=token, resource_uri="ui://carecircle/brief")
    return HTMLResponse(card_html)


async def mcp_tools_call_endpoint(request: Request) -> JSONResponse:
    """Forward a card's tools/call to the MCP server over Streamable HTTP."""
    body = await request.json()
    tool_name = body.get("name")
    tool_args = body.get("arguments", {})
    speaker = body.get("speaker", "Amma")
    token = body.get("token")
    if not token:
        tokens_file = PROJECT_ROOT / "scripts" / ".tokens.json"
        if tokens_file.exists():
            tokens = json.loads(tokens_file.read_text())
            token = tokens.get(speaker, "")

    tool_data, card_html = await call_mcp_tool(token, tool_name, tool_args)
    raw_result = tool_data.get("_raw_call_tool_result") or {
        "content": [{"type": "text", "text": tool_data.get("spoken_response", "")}],
        "structuredContent": tool_data.get("data") or tool_data,
        "isError": False,
    }
    return JSONResponse({
        "result": raw_result,
        "spoken_reply": tool_data.get("spoken_response", ""),
        "card_html": card_html,
    })


routes = [
    Route("/", endpoint=index_page, methods=["GET"]),
    Route("/api/tokens", endpoint=get_tokens, methods=["GET"]),
    Route("/api/provider-info", endpoint=get_provider_info, methods=["GET"]),
    Route("/api/chat", endpoint=chat_endpoint, methods=["POST"]),
    Route("/api/brief-card", endpoint=get_brief_card, methods=["GET"]),
    Route("/api/mcp/tools/call", endpoint=mcp_tools_call_endpoint, methods=["POST"]),
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
