"""Expose the ci_agent Workflow as a local MCP server (stdio transport).

This is the MCP counterpart of engine_entrypoint.py: that module wraps the
graph as an Agent Runtime A2A service; this one wraps `root_agent` for an
MCP host (Claude Code, an IDE) on the same machine. `to_mcp_server` runs the
Workflow in-process against a local Runner (`node=root_agent`).

The single exposed tool takes the same JSON payload shape
deploy/invoke_agent.py already sends the deployed engine, e.g.:
  {"trigger_source": "TEST_OPERATOR", "repo": "agent-substrate/substrate",
   "run_id": "33259998849"}
because that is what TriggerEvent (the graph's input_schema) validates
against - free text is not a valid entry payload for this graph.
"""

from pathlib import Path

from ..env import load_dotenv

# Must run before `.agent` (-> `.nodes`) is imported: nodes.py reads
# CI_AGENT_MODEL/CI_AGENT_MAX_TURNS/etc. from os.environ at import time, and
# unlike `task investigate`, an MCP host launches this process directly - no
# Taskfile dotenv loading happens on its behalf. Resolved from this file's
# location, not cwd: an MCP host's working directory is not guaranteed to be
# the repo root the way Taskfile-driven invocations are.
_REPO_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(_REPO_ROOT / ".env")

from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService
from google.adk.auth.credential_service.in_memory_credential_service import (
    InMemoryCredentialService,
)
from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.tools.mcp_tool import to_mcp_server

from .agent import root_agent

root_agent.description = (
    "Investigates one failed CI run on agent-substrate/substrate: groups "
    "failures, reads logs and source via GitHub, and returns a verdict "
    "(introduced-by-commit / pre-existing / flake / infra) with cited "
    "evidence. Call with the same JSON payload shape as a TriggerEvent, "
    "e.g. {\"trigger_source\": \"TEST_OPERATOR\", \"repo\": \"owner/repo\", "
    "\"run_id\": \"12345\"}."
)

runner = Runner(
    app_name=root_agent.name,
    node=root_agent,
    artifact_service=InMemoryArtifactService(),
    session_service=InMemorySessionService(),
    memory_service=InMemoryMemoryService(),
    credential_service=InMemoryCredentialService(),
)

server = to_mcp_server(
    root_agent,
    name="ci_agent",
    instructions="Investigates a failed CI run and returns a verdict with evidence.",
    runner=runner,
)

if __name__ == "__main__":
    server.run(transport="stdio")
