import logging
import os
from dataclasses import dataclass
from typing import AsyncGenerator, AsyncIterator, Optional
from uuid import uuid4

from agents.result import StreamEvent
from databricks.sdk import WorkspaceClient
from databricks_openai.agents import AsyncDatabricksSession
from mlflow.genai.agent_server import get_request_headers
from mlflow.types.responses import ResponsesAgentRequest, ResponsesAgentStreamEvent
from uuid_utils import uuid7

from agent_server.history import normalize_history_items


@dataclass(frozen=True)
class LakebaseConfig:
    autoscaling_endpoint: Optional[str]
    autoscaling_project: Optional[str]
    autoscaling_branch: Optional[str]
    memory_schema: Optional[str] = None

    @property
    def description(self) -> str:
        return self.autoscaling_endpoint or f"{self.autoscaling_project}/{self.autoscaling_branch}"


def init_lakebase_config() -> LakebaseConfig:
    """Load Lakebase session storage configuration from the environment."""
    endpoint = os.getenv("LAKEBASE_AUTOSCALING_ENDPOINT") or None
    project = os.getenv("LAKEBASE_AUTOSCALING_PROJECT") or None
    branch = os.getenv("LAKEBASE_AUTOSCALING_BRANCH") or None

    if not endpoint and not (project and branch):
        raise ValueError(
            "Lakebase configuration is required. Set LAKEBASE_AUTOSCALING_ENDPOINT "
            "or both LAKEBASE_AUTOSCALING_PROJECT and LAKEBASE_AUTOSCALING_BRANCH."
        )

    memory_schema = os.getenv("LAKEBASE_AGENT_MEMORY_SCHEMA") or None
    if endpoint:
        return LakebaseConfig(endpoint, None, None, memory_schema)
    return LakebaseConfig(None, project, branch, memory_schema)


lakebase_config = init_lakebase_config()


def get_lakebase_access_error_message(lakebase_description: str) -> str:
    """Return an actionable error without exposing database credentials."""
    if os.getenv("DATABRICKS_APP_NAME"):
        return (
            f"Failed to connect to Lakebase '{lakebase_description}'. "
            "Verify the app service principal has CAN_CONNECT_AND_CREATE and "
            "the required schema/table privileges."
        )
    return (
        f"Failed to connect to Lakebase '{lakebase_description}'. "
        "Verify the endpoint, Databricks authentication, and database privileges."
    )


def get_session_id(request: ResponsesAgentRequest) -> str:
    """Reuse the caller's conversation identifier or create a new session."""
    custom_inputs = dict(request.custom_inputs or {})
    if custom_inputs.get("session_id"):
        return str(custom_inputs["session_id"])
    if request.context and request.context.conversation_id:
        return str(request.context.conversation_id)
    return str(uuid7())


def get_databricks_host(workspace_client: WorkspaceClient | None = None) -> Optional[str]:
    workspace_client = workspace_client or WorkspaceClient()
    try:
        return workspace_client.config.host
    except Exception as e:
        logging.exception(f"Error getting databricks host from env: {e}")
        return None


def build_mcp_url(path: str, workspace_client: WorkspaceClient | None = None) -> str:
    if not path.startswith("/"):
        return path
    hostname = get_databricks_host(workspace_client)
    return f"{hostname}{path}"


def get_user_workspace_client() -> WorkspaceClient:
    token = get_request_headers().get("x-forwarded-access-token")
    return WorkspaceClient(token=token, auth_type="pat")


async def deduplicate_input(
    request: ResponsesAgentRequest, session: AsyncDatabricksSession
) -> list[dict]:
    """Avoid replaying history already persisted in the Lakebase session."""
    messages = normalize_history_items([item.model_dump() for item in request.input])
    session_items = await session.get_items()
    if messages and len(session_items) >= len(messages) - 1:
        return [messages[-1]]
    return messages


async def process_agent_stream_events(
    async_stream: AsyncIterator[StreamEvent],
) -> AsyncGenerator[ResponsesAgentStreamEvent, None]:
    curr_item_id = str(uuid4())
    async for event in async_stream:
        if event.type == "raw_response_event":
            event_data = event.data.model_dump()
            if event_data["type"] == "response.output_item.added":
                curr_item_id = str(uuid4())
                event_data["item"]["id"] = curr_item_id
            elif event_data.get("item") is not None and event_data["item"].get("id") is not None:
                event_data["item"]["id"] = curr_item_id
            elif event_data.get("item_id") is not None:
                event_data["item_id"] = curr_item_id
            yield event_data
        elif event.type == "run_item_stream_event" and event.item.type == "tool_call_output_item":
            yield ResponsesAgentStreamEvent(
                type="response.output_item.done",
                item=event.item.to_input_item(),
            )
