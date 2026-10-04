"""Front door over environment-intelligence (BigQuery Data Agent) and
e2e-agent. The two Agent Runtime engines talk through a direct stream_query
call via the Agent Platform SDK, the same call Gemini Enterprise makes under
ADK registration.
"""

import json
import os

import google.auth
from google.adk.agents import Agent
from google.adk.models import Gemini
from google.adk.tools import FunctionTool
from google.adk.tools.data_agent.config import DataAgentToolConfig
from google.adk.tools.data_agent.credentials import DataAgentCredentialsConfig
from google.adk.tools.data_agent.data_agent_toolset import DataAgentToolset
from google.genai import types as genai_types

import agentplatform

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
AGENT_LOCATION = os.environ.get("AGENT_LOCATION", "us-central1")

# Same model env vars as e2e-agent, so one .env covers both agents.
# CI_AGENT_LOCATION overrides GOOGLE_CLOUD_LOCATION for model calls only.
MODEL = os.environ.get("CI_AGENT_MODEL", "gemini-3.7-flash")
MODEL_LOCATION = os.environ.get("CI_AGENT_LOCATION") or os.environ.get("GOOGLE_CLOUD_LOCATION") or "global"

model_adapter = Gemini(
    model=MODEL,
    client_kwargs={"location": MODEL_LOCATION},
    retry_options=genai_types.HttpRetryOptions(attempts=3, initial_delay=2.0, max_delay=30.0),
)

# Resource name of the deployed e2e-agent reasoning engine.
E2E_AGENT_ENGINE = os.environ.get("E2E_AGENT_ENGINE", "")

# The environment-intelligence BigQuery Conversational Analytics agent.
ENV_INTELLIGENCE_AGENT = os.environ.get("ENV_INTELLIGENCE_AGENT", "")


def run_e2e_agent(repo: str, run_id: str) -> str:
    """Ask e2e-agent to investigate a failed GitHub Actions run.

    Use this when the user wants a new investigation of a specific run.
    Questions about history or trends go to ask_data_agent against
    environment-intelligence.

    Args:
      repo: GitHub repo as "owner/repo".
      run_id: The failed workflow run id.

    Returns:
      The text of every event the investigation emitted, concatenated.
      A report that ends by asking the human to finish or dig deeper is
      e2e-agent pausing for a human (dormancy_gate). Relay that text to
      the user verbatim.
    """
    if not PROJECT_ID:
        return "GOOGLE_CLOUD_PROJECT is not set."
    if not E2E_AGENT_ENGINE:
        return "E2E_AGENT_ENGINE is not set."
    runtime_client = agentplatform.Client(project=PROJECT_ID, location=AGENT_LOCATION)
    engine = runtime_client.runtimes.get(name=E2E_AGENT_ENGINE)
    # TriggerEvent.trigger_source only accepts "TEST_OPERATOR" or
    # "SLACK_MANUAL" (src/ci_agent/agent/state.py). It also doubles as the
    # ADK route label out of pin_run_context_node. A coordinator-triggered
    # single-run investigation uses TEST_OPERATOR.
    payload = json.dumps({
        "trigger_source": "TEST_OPERATOR",
        "repo": repo,
        "run_id": run_id,
    })
    # Text parts only. The coordinator relays e2e-agent's report, not tool payloads.
    texts = []
    for event in engine.stream_query(
        user_id=f"sre-coordinator:{repo}:{run_id}", message=payload
    ):
        if not isinstance(event, dict):
            continue
        for part in (event.get("content") or {}).get("parts") or []:
            text = part.get("text")
            if text:
                texts.append(text)
    report = "\n".join(texts).strip()
    return report[:8000] if report else (
        "Investigation finished with no reportable text. "
        "Check the e2e-agent session for the raw trace."
    )


_adc, _ = google.auth.default()
_data_agent_toolset = DataAgentToolset(
    credentials_config=DataAgentCredentialsConfig(credentials=_adc),
    data_agent_tool_config=DataAgentToolConfig(max_query_result_rows=100),
    tool_filter=["list_accessible_data_agents", "get_data_agent_info", "ask_data_agent"],
)

root_agent = Agent(
    name="sre_coordinator",
    model=model_adapter,
    description=(
        "Front door for environment health: answers questions over history "
        "via environment-intelligence, and investigates a specific failed "
        "run via e2e-agent."
    ),
    instruction=f"""You are the SRE coordinator.

You have two capabilities. Pick the right one per request:

1. Questions about history, trends, flaky tests, or past investigations:
   call `ask_data_agent` with data_agent_name="{ENV_INTELLIGENCE_AGENT}" and
   the user's question in natural language.

2. A request to investigate a specific failed run: call `run_e2e_agent` with
   the repo and run_id. If the result ends in a report asking the human to
   choose finish or dig_deeper, show that report and question to the user
   exactly as returned.
""",
    tools=[_data_agent_toolset, FunctionTool(run_e2e_agent)],
)
