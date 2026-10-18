"""Front door over environment-intelligence (BigQuery Data Agent) and
e2e-agent, which investigates failed e2e runs from GitHub Actions. The two
Agent Runtime engines talk via stream_query, the same call Gemini Enterprise
makes under ADK registration. No A2A: that machinery is for crossing
framework, org, or network boundaries.
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

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
AGENT_LOCATION = os.environ.get("AGENT_LOCATION", "us-central1")

# Same model env vars as e2e-agent, so one .env covers both agents.
# E2E_AGENT_LOCATION overrides GOOGLE_CLOUD_LOCATION for model calls only.
MODEL = os.environ.get("E2E_AGENT_MODEL", "gemini-3.7-flash")
MODEL_LOCATION = os.environ.get("E2E_AGENT_LOCATION") or os.environ.get("GOOGLE_CLOUD_LOCATION") or "global"

model_adapter = Gemini(
    model=MODEL,
    client_kwargs={"location": MODEL_LOCATION},
    retry_options=genai_types.HttpRetryOptions(attempts=3, initial_delay=2.0, max_delay=30.0),
)


def _e2e_engine_name() -> str:
    """Resource name of the deployed e2e-agent.

    E2E_AGENT_ENGINE overrides the lookup. Otherwise the display name
    `e2e-agent` is the identity.
    """
    explicit = os.environ.get("E2E_AGENT_ENGINE", "").strip()
    if explicit:
        return explicit
    if not PROJECT_ID:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT is not set.")
    client = agentplatform.Client(project=PROJECT_ID, location=AGENT_LOCATION)
    for runtime in client.runtimes.list():
        if getattr(runtime.api_resource, "display_name", None) == "e2e-agent":
            return runtime.api_resource.name
    raise RuntimeError(
        "no Agent Runtime named e2e-agent; set E2E_AGENT_ENGINE to its resource name"
    )


# Resource name of the environment-intelligence BigQuery data agent.
# Required for ask_data_agent. No default: a resource name is project-specific.
ENV_INTELLIGENCE_AGENT = os.environ.get("ENV_INTELLIGENCE_AGENT", "").strip()


def investigate_e2e_failure(repo: str, run_id: str) -> str:
    """Trigger e2e-agent to investigate a failed e2e run from GitHub Actions.

    Use this when the user wants a NEW investigation of a specific run, not
    when they're asking about history or trends - for that, use
    ask_data_agent against environment-intelligence instead.

    Args:
      repo: GitHub repo as "owner/repo".
      run_id: The failed workflow run id.

    Returns:
      The natural-language text of every event the investigation emitted,
      concatenated. If this ends in a report plus a finish/dig_deeper
      question, that is e2e-agent pausing for a human (dormancy_gate) -
      relay it to the user verbatim rather than answering on their behalf.
      Resuming that paused run is not wired yet; say so if the user tries
      to answer it.
    """
    if not PROJECT_ID:
        return "GOOGLE_CLOUD_PROJECT is not set."
    try:
        engine_name = _e2e_engine_name()
    except RuntimeError as exc:
        return str(exc)
    client = agentplatform.Client(project=PROJECT_ID, location=AGENT_LOCATION)
    engine = client.runtimes.get(name=engine_name)
    # TriggerEvent.trigger_source only accepts "TEST_OPERATOR" or
    # "SLACK_MANUAL" (src/e2e_agent/agent/state.py) - it also doubles as the
    # ADK route label out of pin_run_context_node, so it's not a plain enum
    # we can extend from here. A coordinator-triggered single-run
    # investigation is the same shape as TEST_OPERATOR's.
    payload = json.dumps({
        "trigger_source": "TEST_OPERATOR",
        "repo": repo,
        "run_id": run_id,
    })
    # Only text parts, not the raw functionCall/functionResponse parts -
    # those can carry a single tool's entire log dump (e.g.
    # list_failed_jobs's 7000+ line output) and crashed the google-genai
    # client when fed back in as this tool's own function response
    # (UnknownApiResponseError: Failed to parse response as JSON). The text
    # parts are the investigator's actual authored report anyway - that's
    # what a human (or this coordinator) needs to relay, not tool payloads.
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
        "Investigation finished with no reportable text - check "
        "e2e-agent's own session for the raw trace."
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
        "Single front door for CI/CD environment health: answers questions "
        "over failure history via environment-intelligence, and can trigger "
        "a live investigation of a specific failed run via e2e-agent."
    ),
    instruction=f"""You are the SRE coordinator for this project's CI/CD environment.

You have two distinct capabilities - pick the right one per request:

1. Questions about history, trends, flaky tests, or past investigations:
   call `ask_data_agent` with data_agent_name="{ENV_INTELLIGENCE_AGENT}" and
   the user's question in natural language.

2. "Investigate this failure" / "why did run <id> fail" for a run that
   hasn't been investigated yet: call `investigate_e2e_failure` with the repo
   and run_id. If the result ends in a report asking the human to choose
   finish or dig_deeper, show that report and question to the user exactly
   as returned - do not summarize away the choice, and do not decide it
   yourself. Tell the user that resuming the paused investigation with their
   answer isn't wired up yet in this POC.
""",
    tools=[_data_agent_toolset, FunctionTool(investigate_e2e_failure)],
)
