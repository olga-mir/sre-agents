"""Optional A2A wrapper. Not the Agent Runtime deploy entrypoint.

The container serves `AdkApp` (`google-adk`) so traces, playground, and
Gemini Enterprise `:streamQuery` stay enabled. This module keeps a
constructible `A2aAgent` for tests and a possible later Cloud Run A2A
surface. Do not bind it as the Agent Runtime entrypoint.
"""

from __future__ import annotations

import os

from a2a.types import AgentSkill
from google.adk.a2a.converters.request_converter import (
    convert_a2a_request_to_agent_run_request,
)
from google.adk.a2a.executor.a2a_agent_executor import A2aAgentExecutor
from google.adk.a2a.executor.config import A2aAgentExecutorConfig
from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService
from google.adk.auth.credential_service.in_memory_credential_service import (
    InMemoryCredentialService,
)
from google.adk.memory.in_memory_memory_service import InMemoryMemoryService
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.genai import types as genai_types
from agentplatform.frameworks.a2a import A2aAgent, create_agent_card

from .agent import app, root_agent
from .trigger_parse import USAGE, parse_trigger_text

os.environ.setdefault("ADK_SUPPRESS_A2A_EXPERIMENTAL_FEATURE_WARNINGS", "true")
# A2aAgent reads project and location from the environment at set_up().
os.environ.setdefault(
    "GOOGLE_CLOUD_LOCATION",
    os.environ.get("AGENT_LOCATION", "us-central1"),
)

root_agent.description = (
    "Investigates one failed GitHub Actions run: groups failures, reads logs "
    "and source, and returns a verdict with cited evidence."
)

investigate_skill = AgentSkill(
    id="investigate_e2e_run",
    name="Run e2e-agent",
    description=(
        "Investigate a failed GitHub Actions workflow run. Requires repo "
        "(owner/name) and run_id. Accepts TriggerEvent JSON, a github.com "
        "actions/runs URL, or prose naming both."
    ),
    tags=["ci", "sre", "github-actions"],
    examples=[
        '{"trigger_source":"TEST_OPERATOR","repo":"agent-substrate/substrate","run_id":"33259998849"}',
        "https://github.com/agent-substrate/substrate/actions/runs/33259998849",
        "investigate agent-substrate/substrate run 33259998849",
    ],
)

agent_card = create_agent_card(
    agent_name="e2e-agent",
    description=(
        "SRE agent that investigates CI/E2E failures on GitHub Actions: "
        "workflow output, source, and PR diff."
    ),
    skills=[investigate_skill],
)


def _runner() -> Runner:
    return Runner(
        app_name=app.name,
        node=root_agent,
        artifact_service=InMemoryArtifactService(),
        session_service=InMemorySessionService(),
        memory_service=InMemoryMemoryService(),
        credential_service=InMemoryCredentialService(),
    )


def _request_converter(request, part_converter=None):
    kwargs = {}
    if part_converter is not None:
        kwargs["part_converter"] = part_converter
    run_req = convert_a2a_request_to_agent_run_request(request, **kwargs)
    parts = list(run_req.new_message.parts or []) if run_req.new_message else []
    text = "".join(getattr(part, "text", None) or "" for part in parts)
    trigger = parse_trigger_text(text)
    if trigger is None:
        raise ValueError(USAGE)
    run_req.new_message = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=trigger.model_dump_json())],
    )
    return run_req


def _executor_builder(**_kwargs):
    return A2aAgentExecutor(
        runner=_runner,
        config=A2aAgentExecutorConfig(request_converter=_request_converter),
    )


a2a_agent = A2aAgent(
    agent_card=agent_card,
    agent_executor_builder=_executor_builder,
)
