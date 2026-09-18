"""Genie **agent mode** (agentic) REST client + OpenAI Agents SDK tools.

Agent mode drives a Genie space through its Responses-style API
(``POST /api/2.0/genie/agents/{agent_id}/responses``), which runs Genie
*agentically* — e.g. reasoning over attached PDF context files — unlike the MCP
"chat mode" the template used before. ``agent_id`` is synonymous with the Genie
space id.

The client (whoever deploys the app) passes one or more Genie space ids as
configuration; :func:`build_genie_agent_tools` turns each into a function tool
that the orchestrator LLM can call, sending the user's question to the matching
Genie space "as if it were a tool".

Configuration (checked in order):
  * ``GENIE_AGENTS``    — JSON list of ``{"id", "name"?, "description"?}`` (or bare id strings)
  * ``GENIE_AGENT_IDS`` — comma-separated space ids
  * ``GENIE_SPACE_ID``  — single space id (backwards compatible)

Endpoints wrapped (see https://docs.databricks.com/api/genie/v1/):
  * create_agent_response        POST   /api/2.0/genie/agents/{agent_id}/responses               (SSE)
  * list_conversation_items      GET    /api/2.0/genie/agents/{agent_id}/conversations/{cid}/items
  * cancel_response              POST   /api/2.0/genie/agents/{agent_id}/conversations/{cid}/responses/{rid}/cancel
  * get_conversation_message     GET    /api/2.0/genie/spaces/{space_id}/conversations/{cid}/messages/{mid}
  * list_conversation_messages   GET    /api/2.0/genie/spaces/{space_id}/conversations/{cid}/messages
  * list_message_comments        GET    /api/2.0/genie/spaces/{space_id}/conversations/{cid}/messages/{mid}/comments
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from agents import FunctionTool
from agents.tool_context import ToolContext
from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 300.0


# ---------------------------------------------------------------------------
# Auth / HTTP helpers
# ---------------------------------------------------------------------------


def _auth(workspace_client: Optional[WorkspaceClient]) -> tuple[str, dict[str, str]]:
    """Return (host, auth_headers) for raw Genie API calls.

    Works for any Databricks auth the app is configured with (service principal
    OAuth, PAT, or an on-behalf-of user token via a passed WorkspaceClient).
    """
    w = workspace_client or WorkspaceClient()
    host = (w.config.host or "").rstrip("/")
    headers = dict(w.config.authenticate() or {})
    return host, headers


def obo_workspace_client() -> Optional[WorkspaceClient]:
    """Return a WorkspaceClient authenticated as the requesting user (OBO), if a
    forwarded user token is present; otherwise None (caller falls back to the
    app service principal).

    With OBO, Genie queries run with the *user's* permissions, so the app does
    not need to declare each Genie space as an app resource or grant the service
    principal access per space.
    """
    try:
        from mlflow.genai.agent_server import get_request_headers

        token = (get_request_headers() or {}).get("x-forwarded-access-token")
        if token:
            return WorkspaceClient(token=token, auth_type="pat")
    except Exception:
        logger.debug("No OBO user token available; using default credentials.", exc_info=True)
    return None


def _extract_assistant_text(output: list[dict] | None) -> str:
    """Pull the assistant's narrative text out of an agent-mode ``output`` array."""
    parts: list[str] = []
    for item in output or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message" and item.get("role") == "assistant":
            for chunk in item.get("content") or []:
                if isinstance(chunk, dict) and chunk.get("type") in ("output_text", "text"):
                    text = chunk.get("text")
                    if text:
                        parts.append(text)
    return "\n".join(parts).strip()


async def _iter_sse(response: httpx.Response):
    """Yield (event_name, parsed_json) tuples from a text/event-stream response."""
    event_name: str | None = None
    data_lines: list[str] = []
    async for raw in response.aiter_lines():
        line = raw.rstrip("\r")
        if line == "":  # blank line dispatches the accumulated event
            if data_lines:
                payload = "\n".join(data_lines)
                try:
                    yield event_name, json.loads(payload)
                except json.JSONDecodeError:
                    yield event_name, None
            event_name, data_lines = None, []
            continue
        if line.startswith(":"):  # comment / heartbeat
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)
    if data_lines:  # flush a trailing event with no terminating blank line
        payload = "\n".join(data_lines)
        try:
            yield event_name, json.loads(payload)
        except json.JSONDecodeError:
            yield event_name, None


# ---------------------------------------------------------------------------
# Agent-mode API functions
# ---------------------------------------------------------------------------


async def create_agent_response(
    agent_id: str,
    text: str,
    *,
    conversation_id: str | None = None,
    enable_viz: bool = False,
    workspace_client: Optional[WorkspaceClient] = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Ask an agent-mode Genie a question and return the final result.

    Consumes the SSE stream to completion. Returns a dict with:
    ``text`` (assistant answer), ``conversation_id``, ``response_id``,
    ``status`` ("completed"/"failed"/...), and ``error`` (when failed).

    Omit ``conversation_id`` to start a new conversation; pass it to continue one.
    """
    host, headers = _auth(workspace_client)
    url = f"{host}/api/2.0/genie/agents/{agent_id}/responses"
    body: dict[str, Any] = {
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}
        ]
    }
    if conversation_id:
        body["conversation_id"] = conversation_id
    if enable_viz:
        body["enable_viz"] = True
    headers = {**headers, "Content-Type": "application/json", "Accept": "text/event-stream"}

    result: dict[str, Any] = {
        "text": "",
        "conversation_id": conversation_id,
        "response_id": None,
        "status": "in_progress",
        "error": None,
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, headers=headers, json=body) as response:
            if response.status_code >= 400:
                detail = (await response.aread()).decode("utf-8", "replace")
                raise httpx.HTTPStatusError(
                    f"Genie agent {agent_id} responded {response.status_code}: {detail}",
                    request=response.request,
                    response=response,
                )

            content_type = response.headers.get("content-type", "")
            if "text/event-stream" not in content_type:
                # Non-streaming fallback: a single JSON response object.
                payload = json.loads((await response.aread()).decode("utf-8", "replace"))
                resp = payload.get("response", payload)
                result["response_id"] = resp.get("id")
                result["conversation_id"] = resp.get("conversation_id", conversation_id)
                result["status"] = resp.get("status", "completed")
                result["error"] = resp.get("error")
                result["text"] = _extract_assistant_text(resp.get("output"))
                return result

            async for event_name, data in _iter_sse(response):
                if not data:
                    continue
                etype = data.get("type") or event_name
                resp = data.get("response", {}) if isinstance(data.get("response"), dict) else {}
                if etype == "response.created":
                    result["response_id"] = resp.get("id") or result["response_id"]
                    result["conversation_id"] = resp.get("conversation_id") or result["conversation_id"]
                elif etype == "response.completed":
                    result["status"] = resp.get("status", "completed")
                    result["conversation_id"] = resp.get("conversation_id") or result["conversation_id"]
                    result["text"] = _extract_assistant_text(resp.get("output"))
                elif etype == "response.failed":
                    result["status"] = "failed"
                    result["error"] = resp.get("error")

    return result


async def list_conversation_items(
    agent_id: str,
    conversation_id: str,
    *,
    limit: int = 100,
    after: str | None = None,
    order: str = "asc",
    workspace_client: Optional[WorkspaceClient] = None,
) -> dict[str, Any]:
    """GET the polymorphic items (messages, reasoning, function calls) of a conversation."""
    host, headers = _auth(workspace_client)
    url = f"{host}/api/2.0/genie/agents/{agent_id}/conversations/{conversation_id}/items"
    params: dict[str, Any] = {"limit": limit, "order": order}
    if after:
        params["after"] = after
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()


async def cancel_response(
    agent_id: str,
    conversation_id: str,
    response_id: str,
    *,
    workspace_client: Optional[WorkspaceClient] = None,
) -> dict[str, Any]:
    """POST cancel an in-progress agent-mode response."""
    host, headers = _auth(workspace_client)
    url = (
        f"{host}/api/2.0/genie/agents/{agent_id}/conversations/{conversation_id}"
        f"/responses/{response_id}/cancel"
    )
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, headers=headers)
        resp.raise_for_status()
        return resp.json() if resp.content else {}


async def get_conversation_message(
    space_id: str,
    conversation_id: str,
    message_id: str,
    *,
    workspace_client: Optional[WorkspaceClient] = None,
) -> dict[str, Any]:
    """GET a single message from a chat-mode or agent-mode conversation."""
    host, headers = _auth(workspace_client)
    url = f"{host}/api/2.0/genie/spaces/{space_id}/conversations/{conversation_id}/messages/{message_id}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def list_conversation_messages(
    space_id: str,
    conversation_id: str,
    *,
    page_size: int = 20,
    page_token: str | None = None,
    workspace_client: Optional[WorkspaceClient] = None,
) -> dict[str, Any]:
    """GET a page of messages in a conversation."""
    host, headers = _auth(workspace_client)
    url = f"{host}/api/2.0/genie/spaces/{space_id}/conversations/{conversation_id}/messages"
    params: dict[str, Any] = {"page_size": page_size}
    if page_token:
        params["page_token"] = page_token
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()


async def list_message_comments(
    space_id: str,
    conversation_id: str,
    message_id: str,
    *,
    workspace_client: Optional[WorkspaceClient] = None,
) -> dict[str, Any]:
    """GET all comments on a specific message."""
    host, headers = _auth(workspace_client)
    url = (
        f"{host}/api/2.0/genie/spaces/{space_id}/conversations/{conversation_id}"
        f"/messages/{message_id}/comments"
    )
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def get_space_metadata(
    space_id: str, *, workspace_client: Optional[WorkspaceClient] = None
) -> dict[str, Any]:
    """GET a Genie space's metadata (title, description). Best-effort."""
    host, headers = _auth(workspace_client)
    url = f"{host}/api/2.0/genie/spaces/{space_id}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Config parsing + tool building
# ---------------------------------------------------------------------------


@dataclass
class GenieAgentSpec:
    id: str
    name: str | None = None
    description: str | None = None


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", text or "").strip("_").lower()
    return slug or "genie"


def parse_genie_agent_specs() -> list[GenieAgentSpec]:
    """Read the configured Genie agent ids from the environment (see module docstring)."""
    specs: list[GenieAgentSpec] = []

    raw = os.getenv("GENIE_AGENTS")
    if raw:
        try:
            for entry in json.loads(raw):
                if isinstance(entry, str) and entry.strip():
                    specs.append(GenieAgentSpec(id=entry.strip()))
                elif isinstance(entry, dict) and entry.get("id"):
                    specs.append(
                        GenieAgentSpec(
                            id=str(entry["id"]).strip(),
                            name=entry.get("name"),
                            description=entry.get("description"),
                        )
                    )
        except (json.JSONDecodeError, TypeError):
            logger.warning("GENIE_AGENTS is not valid JSON; ignoring it.")

    if not specs:
        ids = os.getenv("GENIE_AGENT_IDS", "")
        specs = [GenieAgentSpec(id=i.strip()) for i in ids.split(",") if i.strip()]

    if not specs:
        single = os.getenv("GENIE_SPACE_ID")
        if single:
            specs = [GenieAgentSpec(id=single.strip())]

    # De-duplicate by id, keeping the first occurrence.
    seen: set[str] = set()
    unique: list[GenieAgentSpec] = []
    for spec in specs:
        if spec.id and spec.id not in seen:
            seen.add(spec.id)
            unique.append(spec)
    return unique


def _make_genie_tool(spec: GenieAgentSpec, tool_name: str, tool_description: str) -> FunctionTool:
    """Build a single FunctionTool that queries one agent-mode Genie space."""
    params_schema = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The natural-language question to ask this Genie space.",
            }
        },
        "required": ["question"],
        "additionalProperties": False,
    }

    async def on_invoke(ctx: ToolContext[Any], args_json: str) -> str:
        try:
            args = json.loads(args_json) if args_json else {}
        except json.JSONDecodeError:
            args = {}
        question = (args.get("question") or "").strip()
        if not question:
            return "No question was provided to the Genie tool."
        try:
            # Prefer OBO (run as the requesting user) so no per-space app resource
            # or service-principal grant is needed; fall back to the app SP.
            result = await create_agent_response(
                spec.id, question, workspace_client=obo_workspace_client()
            )
        except Exception as exc:  # network / auth / API errors surfaced to the model
            logger.warning("Genie agent %s call failed: %s", spec.id, exc, exc_info=True)
            return f"Genie space is unavailable right now: {exc}"
        if result.get("status") == "failed":
            return f"Genie could not answer: {result.get('error') or 'unknown error'}"
        return result.get("text") or "(Genie returned no text answer.)"

    return FunctionTool(
        name=tool_name,
        description=tool_description,
        params_json_schema=params_schema,
        on_invoke_tool=on_invoke,
    )


async def build_genie_agent_tools(
    workspace_client: Optional[WorkspaceClient] = None,
) -> list[FunctionTool]:
    """Build one function tool per configured Genie space (agent mode).

    Tool name/description come from the config when provided, otherwise from the
    Genie space's own title/description (best-effort fetch), otherwise a generic
    fallback derived from the id.
    """
    # Resolve metadata (tool naming) as the requesting user when possible, so a
    # nice name is available even if the service principal can't see the space.
    metadata_client = workspace_client or obo_workspace_client()
    tools: list[FunctionTool] = []
    used_names: set[str] = set()
    for spec in parse_genie_agent_specs():
        title = spec.name
        description = spec.description
        if not title or not description:
            try:
                meta = await get_space_metadata(spec.id, workspace_client=metadata_client)
                title = title or meta.get("title") or meta.get("name")
                description = description or meta.get("description")
            except Exception:
                logger.info("Could not fetch Genie space metadata for %s; using fallback.", spec.id)

        title = title or f"Genie {spec.id[:8]}"
        # Always prefix Genie tools with `genie_` so the tool name the model routes
        # to (and the UI shows) is unambiguous: genie_samsung_manuals,
        # genie_sioas_subsidies, etc. _slug keeps it to a valid [a-z0-9_-] name.
        base_name = f"genie_{_slug(spec.name or title)}"
        name = base_name
        suffix = 2
        while name in used_names:  # guarantee unique tool names
            name = f"{base_name}_{suffix}"
            suffix += 1
        used_names.add(name)

        tool_description = (
            f"Ask the '{title}' Genie space (Databricks agent mode). "
            f"{description or 'Answers natural-language questions over its configured data and documents.'} "
            "Pass a single natural-language 'question'; the Genie space reasons over its "
            "data/PDFs and returns an answer."
        )
        tools.append(_make_genie_tool(spec, name, tool_description))
    return tools
