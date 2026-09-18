"""
Multi-agent orchestrator template.

!! CONFIGURATION REQUIRED !!
This template is NOT ready to run out of the box. You must configure the
placeholder values below before running. Search for "TODO:" to find all
values that need to be set.

This orchestrator uses:
  1. A Genie space for natural-language analysis via Databricks MCP.
  2. An AI Search index for semantic and hybrid retrieval via Databricks MCP.
  3. Lakebase-backed OpenAI Agents SDK sessions for conversation history.
"""

import logging
import os
from contextlib import AsyncExitStack
from typing import AsyncGenerator

import mlflow
from agents import Agent, FunctionTool, Runner, set_default_openai_api, set_default_openai_client
from agents.tracing import set_trace_processors
from databricks_openai import AsyncDatabricksOpenAI
from databricks_openai.agents import AsyncDatabricksSession, McpServer
from fastapi import HTTPException

from agent_server.genie_agent import build_genie_agent_tools, parse_genie_agent_specs
from agent_server.search_tool import build_search_tools
from mlflow.genai.agent_server import invoke, stream
from mlflow.types.responses import (
    ResponsesAgentRequest,
    ResponsesAgentResponse,
    ResponsesAgentStreamEvent,
)

from agent_server.utils import (
    build_mcp_url,
    deduplicate_input,
    get_lakebase_access_error_message,
    get_session_id,
    lakebase_config,
    process_agent_stream_events,
)

# ---------------------------------------------------------------------------
# Client setup
# ---------------------------------------------------------------------------

# Use the Responses API: reasoning models (e.g. gpt-5-6-luna) require it for
# function tools (chat_completions rejects tools unless reasoning_effort='none').
set_default_openai_client(AsyncDatabricksOpenAI())
set_default_openai_api("responses")
set_trace_processors([])  # only use mlflow for trace processing
mlflow.openai.autolog()
logging.getLogger("mlflow.utils.autologging_utils").setLevel(logging.ERROR)
logger = logging.getLogger(__name__)

# MLflow 3.13.0 tracing guard.
# databricks-openai's ``AsyncDatabricksResponses`` autolog span reports a
# non-str span_type (a list like ``['UNKNOWN', None]``). MLflow 3.13.0's
# ``default_log_level_for_span_type`` does ``span_type in <frozenset>`` without
# guarding against an unhashable value, so ending that span raises
# ``TypeError: unhashable type: 'list'`` — which aborts the span and silently
# drops EVERY trace (nothing reaches the experiment). Degrade a bad span_type
# to DEBUG instead of crashing. No-op on MLflow versions without this path.
try:
    import mlflow.entities.span as _mlflow_span

    _orig_default_log_level = _mlflow_span.default_log_level_for_span_type

    def _safe_default_log_level(span_type):
        try:
            return _orig_default_log_level(span_type)
        except TypeError:
            from mlflow.entities.span_log_level import SpanLogLevel

            return SpanLogLevel.DEBUG

    _mlflow_span.default_log_level_for_span_type = _safe_default_log_level
except Exception:  # never block startup on the guard
    logger.debug("MLflow span-type guard not applied", exc_info=True)

# ---------------------------------------------------------------------------
# MCP server + orchestrator agent
# ---------------------------------------------------------------------------


# Both Genie (agent mode) and AI Search are exposed as short-named function
# tools rather than MCP servers: the Vector Search MCP tool is auto-named after
# the index full name (69 chars), which exceeds the model API's 64-char limit on
# tool/function names. Genie chat mode still uses MCP (build_genie_mcp_servers).


# Genie agent-mode tools are built once and reused across requests (the
# configured space ids and their metadata are stable for the app's lifetime).
_genie_tools_cache: list[FunctionTool] | None = None


async def get_genie_tools() -> list[FunctionTool]:
    """Return the configured Genie agent-mode function tools (cached)."""
    global _genie_tools_cache
    if _genie_tools_cache is None:
        _genie_tools_cache = await build_genie_agent_tools()
    return _genie_tools_cache


def build_genie_mcp_servers() -> list[McpServer]:
    """Build one Genie MCP server per configured space (CHAT mode).

    Used when High Effort is OFF: Genie runs as a chat-mode MCP tool over the
    same client-provided space ids, instead of the agent-mode REST tools.
    """
    servers: list[McpServer] = []
    for spec in parse_genie_agent_specs():
        servers.append(
            McpServer(
                url=build_mcp_url(f"/api/2.0/mcp/genie/{spec.id}"),
                name=spec.name or f"Genie {spec.id[:8]}",
            )
        )
    return servers


def resolve_high_effort(request: ResponsesAgentRequest) -> bool:
    """Whether to use Genie AGENT mode (True) or CHAT mode (False).

    Per-request override via ``custom_inputs.high_effort``; otherwise the
    ``GENIE_HIGH_EFFORT`` env default (default: True / agent mode).
    """
    custom_inputs = getattr(request, "custom_inputs", None) or {}
    if isinstance(custom_inputs, dict) and custom_inputs.get("high_effort") is not None:
        return bool(custom_inputs["high_effort"])
    return os.getenv("GENIE_HIGH_EFFORT", "true").strip().lower() not in ("false", "0", "no", "off")


async def connect_healthy_mcp_servers(
    stack: AsyncExitStack, servers: list[McpServer]
) -> tuple[list[McpServer], list[str]]:
    """Connect each MCP server and verify it can actually list its tools.

    The Agents SDK lists each server's tools lazily inside ``Runner.run``, so a server that
    connects but fails at list time (e.g. an unauthorized Genie space) would otherwise crash
    the whole request — including the unrelated subagent tools. We list tools here, per
    server: healthy servers are kept; any that fails to connect OR to list is dropped and its
    name returned, so the orchestrator runs with whatever is available instead of erroring out.

    Returns (healthy_servers, unavailable_names).
    """
    healthy: list[McpServer] = []
    unavailable: list[str] = []
    for server in servers:
        name = getattr(server, "name", "MCP server")
        try:
            connected = await stack.enter_async_context(server)
            await connected.list_tools()  # forces the connectivity + authorization check now
            healthy.append(connected)
        except Exception:
            logger.warning("MCP server %r unavailable; continuing without it.", name, exc_info=True)
            unavailable.append(name)
    return healthy, unavailable


def create_orchestrator_agent(
    mcp_servers: list[McpServer],
    tools: list[FunctionTool] | None = None,
    unavailable_tools: list[str] | None = None,
) -> Agent:
    """Build the orchestrator agent with its function tools and MCP servers."""
    # TODO: Update these instructions to match the tools you keep or add.
    # The more specific the instructions, the more accurately the agent will
    # route requests to the right tool.
    instructions = (
        "You are an orchestrator agent. Route the user's request to the "
        "most appropriate tool or data source:\n"
        "- Use the 'search_manuals' tool to retrieve exact manual passages for "
        "setup, operation, safety, product features, error messages, and troubleshooting. "
        "Include exact model names, codes, or technical terms in the search query when relevant.\n"
        "- Use the available Genie space tools for natural-language analysis, comparisons, "
        "summaries, and questions over their configured data and documents. Pass a clear, "
        "self-contained question and pick the Genie tool whose name/description best matches "
        "the topic.\n"
        "- Use multiple sources when retrieval evidence and structured analysis are both useful. "
        "Ground the answer in tool results and mention the source manual or document when available.\n"
        "- PRESERVE SOURCE LINKS: when a tool result contains citation links to manuals or "
        "documents (markdown links such as `[1](https://...)`, including the trailing "
        "`\\[[n](url)\\]` citation markers Genie appends), carry those links through into your "
        "final answer VERBATIM, next to the claims they support. Do not drop, rewrite, renumber, "
        "or convert them to plain text — the user needs the clickable source links.\n"
        "If the question is outside the available scope, explain that limitation. "
        "If the requested product or issue is ambiguous, ask the user for clarification."
    )
    if unavailable_tools:
        names = ", ".join(sorted(set(unavailable_tools)))
        instructions += (
            f"\n\nThese data sources are currently UNAVAILABLE (not authorized or unreachable "
            f"right now): {names}. If answering requires one of them, briefly tell the user it "
            "isn't available right now instead of guessing, and use whatever else you have."
        )
    return Agent(
        name="Orchestrator",
        instructions=instructions,
        model="databricks-gpt-5-6-luna",
        mcp_servers=mcp_servers,
        tools=tools or [],
    )


def _create_session(session_id: str) -> AsyncDatabricksSession:
    """Create a Lakebase-backed OpenAI Agents SDK session."""
    return AsyncDatabricksSession(
        session_id=session_id,
        autoscaling_endpoint=lakebase_config.autoscaling_endpoint,
        project=lakebase_config.autoscaling_project,
        branch=lakebase_config.autoscaling_branch,
        schema=lakebase_config.memory_schema,
        create_tables=False,
    )


def _raise_lakebase_error(exc: Exception) -> None:
    error_text = str(exc).lower()
    indicators = (
        "lakebase",
        "pg_hba",
        "postgres",
        "database instance",
        "insufficient privilege",
    )
    if any(indicator in error_text for indicator in indicators):
        logger.error("Lakebase session error: %s", exc)
        raise HTTPException(
            status_code=503,
            detail=get_lakebase_access_error_message(lakebase_config.description),
        ) from exc


# ---------------------------------------------------------------------------
# MLflow Responses API handlers
# ---------------------------------------------------------------------------


@invoke()
async def invoke_handler(request: ResponsesAgentRequest) -> ResponsesAgentResponse:
    try:
        session_id = get_session_id(request)
        mlflow.update_current_trace(metadata={"mlflow.trace.session": session_id})
        session = _create_session(session_id)
        high_effort = resolve_high_effort(request)
        async with AsyncExitStack() as stack:
            tools: list[FunctionTool] = build_search_tools()  # AI Search (short-named)
            mcp_servers: list[McpServer] = []
            if high_effort:
                tools = tools + await get_genie_tools()  # Genie agent mode (REST)
            else:
                mcp_servers = build_genie_mcp_servers()  # Genie chat mode (MCP)
            servers, unavailable = await connect_healthy_mcp_servers(stack, mcp_servers)
            agent = create_orchestrator_agent(servers, tools, unavailable)
            messages = await deduplicate_input(request, session)
            result = await Runner.run(agent, messages, session=session)
            return ResponsesAgentResponse(
                output=[item.to_input_item() for item in result.new_items],
                custom_outputs={"session_id": session.session_id},
            )
    except Exception as exc:
        _raise_lakebase_error(exc)
        raise


@stream()
async def stream_handler(request: ResponsesAgentRequest) -> AsyncGenerator[ResponsesAgentStreamEvent, None]:
    try:
        session_id = get_session_id(request)
        mlflow.update_current_trace(metadata={"mlflow.trace.session": session_id})
        session = _create_session(session_id)
        high_effort = resolve_high_effort(request)
        async with AsyncExitStack() as stack:
            tools: list[FunctionTool] = build_search_tools()  # AI Search (short-named)
            mcp_servers: list[McpServer] = []
            if high_effort:
                tools = tools + await get_genie_tools()  # Genie agent mode (REST)
            else:
                mcp_servers = build_genie_mcp_servers()  # Genie chat mode (MCP)
            servers, unavailable = await connect_healthy_mcp_servers(stack, mcp_servers)
            agent = create_orchestrator_agent(servers, tools, unavailable)
            messages = await deduplicate_input(request, session)
            result = Runner.run_streamed(agent, input=messages, session=session)

            async for event in process_agent_stream_events(result.stream_events()):
                yield event
    except Exception as exc:
        _raise_lakebase_error(exc)
        raise
