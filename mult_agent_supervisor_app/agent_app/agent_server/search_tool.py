"""AI Search (Vector Search) exposed as a short-named function tool.

Previously the index was queried via the Databricks Vector Search MCP server,
which names its tool after the index full name. That name
(`stable_classic_6kvrb7_catalog__cesar_cordoba__knowledge_base_vs_index`, 69
chars) exceeds the 64-char limit the model API enforces on tool/function names,
causing a 400 `string_above_max_length` error. Exposing search as a custom
function tool lets us give it a short, stable name (`search_manuals`) and query
the index directly over REST.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

import httpx
from agents import FunctionTool
from agents.tool_context import ToolContext
from databricks.sdk import WorkspaceClient

from agent_server.genie_agent import _auth, obo_workspace_client

logger = logging.getLogger(__name__)

_DEFAULT_COLUMNS = ["chunk_to_embed", "image_uri"]


async def query_index(
    index: str,
    query: str,
    *,
    num_results: int = 5,
    columns: list[str] | None = None,
    workspace_client: Optional[WorkspaceClient] = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Query a Databricks Vector Search index by text and return the raw payload."""
    host, headers = _auth(workspace_client)
    url = f"{host}/api/2.0/vector-search/indexes/{index}/query"
    body = {
        "query_text": query,
        "columns": columns or _DEFAULT_COLUMNS,
        "num_results": num_results,
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            url, headers={**headers, "Content-Type": "application/json"}, json=body
        )
        resp.raise_for_status()
        return resp.json()


def _format_results(payload: dict[str, Any]) -> str:
    """Turn a Vector Search query payload into readable text passages."""
    columns = [c.get("name") for c in payload.get("manifest", {}).get("columns", [])]
    rows = payload.get("result", {}).get("data_array", []) or []
    passages: list[str] = []
    for row in rows:
        record = dict(zip(columns, row))
        text = (record.get("chunk_to_embed") or "").strip()
        if not text:
            continue
        image = record.get("image_uri")
        if image:
            text += f"\n[image: {image}]"
        passages.append(text)
    return "\n\n---\n\n".join(passages) if passages else "No matching passages found."


def build_search_tools(index_name: Optional[str] = None) -> list[FunctionTool]:
    """Build the AI Search function tool(s). Empty if no index is configured."""
    index = index_name or os.getenv("VECTOR_SEARCH_INDEX")
    if not index:
        return []

    params_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Natural-language search query. Include exact model names, codes, "
                    "or technical terms when relevant."
                ),
            }
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    async def on_invoke(ctx: ToolContext[Any], args_json: str) -> str:
        try:
            args = json.loads(args_json) if args_json else {}
        except json.JSONDecodeError:
            args = {}
        query = (args.get("query") or "").strip()
        if not query:
            return "No search query was provided."
        try:
            payload = await query_index(index, query, workspace_client=obo_workspace_client())
        except Exception as exc:  # network / auth / API errors surfaced to the model
            logger.warning("Vector search on %s failed: %s", index, exc, exc_info=True)
            return f"AI Search is unavailable right now: {exc}"
        return _format_results(payload)

    return [
        FunctionTool(
            name="search_manuals",
            description=(
                "Semantic search over the parsed Samsung manuals (AI Search index). "
                "Returns the most relevant manual passages (and image references) for "
                "setup, operation, safety, product features, error messages, and "
                "troubleshooting. Pass a natural-language 'query'."
            ),
            params_json_schema=params_schema,
            on_invoke_tool=on_invoke,
        )
    ]
