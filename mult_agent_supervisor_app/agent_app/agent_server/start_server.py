import logging
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from databricks_openai.agents import AsyncDatabricksSession
from mlflow.genai.agent_server import AgentServer, setup_mlflow_git_based_version_tracking

# Load env vars from .env before importing the agent for proper auth
load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env", override=True)

# Need to import the agent to register the functions with the server
import agent_server.agent  # noqa: E402
from agent_server.utils import (  # noqa: E402
    get_lakebase_access_error_message,
    lakebase_config,
)

agent_server = AgentServer("ResponsesAgent", enable_chat_proxy=True)


async def run_lakebase_session_setup() -> None:
    """Create the OpenAI Agents SDK session tables once at startup."""
    session = AsyncDatabricksSession(
        session_id="__startup__",
        autoscaling_endpoint=lakebase_config.autoscaling_endpoint,
        project=lakebase_config.autoscaling_project,
        branch=lakebase_config.autoscaling_branch,
        schema=lakebase_config.memory_schema,
    )
    await session._ensure_tables()


_original_lifespan = agent_server.app.router.lifespan_context


@asynccontextmanager
async def _lifespan(app):
    try:
        await run_lakebase_session_setup()
    except Exception as exc:
        logging.getLogger(__name__).error(
            "Lakebase session setup failed: %s\n%s",
            exc,
            get_lakebase_access_error_message(lakebase_config.description),
        )
        raise
    async with _original_lifespan(app):
        yield


agent_server.app.router.lifespan_context = _lifespan

# Define the app as a module level variable to enable multiple workers
app = agent_server.app  # noqa: F841
setup_mlflow_git_based_version_tracking()


def main():
    agent_server.run(app_import_string="agent_server.start_server:app")
