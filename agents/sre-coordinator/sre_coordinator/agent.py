"""Front door over environment-intelligence (BigQuery Data Agent) and
e2e-agent, which investigates failed e2e runs from GitHub Actions. The two
Agent Runtime engines talk via stream_query, the same call Gemini Enterprise
makes under ADK registration. No A2A: that machinery is for crossing
framework, org, or network boundaries.
"""

import os

import google.auth
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.tools import FunctionTool, ToolContext
from google.adk.tools.data_agent.config import DataAgentToolConfig
from google.adk.tools.data_agent.credentials import DataAgentCredentialsConfig
from google.adk.tools.data_agent.data_agent_toolset import DataAgentToolset
from google.genai import types as genai_types

import agentplatform

from . import e2e_edge

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


_STATE_KEY = "e2e_investigations"  # {run_id: {session_id, interrupt_id, repo}}


def _failed(report: str) -> dict:
    return {"status": "failed", "report": report, "pending": None}


def _engine():
    client = agentplatform.Client(project=PROJECT_ID, location=AGENT_LOCATION)
    return client.runtimes.get(name=_e2e_engine_name())


def _user_id(repo: str, run_id: str) -> str:
    return f"sre-coordinator:{repo}:{run_id}"


def _record_handle(tool_context: ToolContext, repo: str, run_id: str,
                   result: e2e_edge.E2EResult) -> None:
    """Record, in coordinator session state, how to resume run_id's remote investigation.

    The tools only receive run_id from the model; the remote e2e-agent
    session_id and interrupt_id must come from somewhere the model cannot
    garble, and must survive across turns. Keyed by run_id so several paused
    investigations can coexist in one coordinator session.

    If result is paused (needs_input), store/overwrite the handle for run_id.
    Otherwise (done or failed) drop it: nothing is left to resume. Not called
    when the remote call raises, so a transient error leaves the previous
    handle in place.
    """
    runs = dict(tool_context.state.get(_STATE_KEY) or {})
    if result["status"] == "needs_input" and result["pending"]:
        runs[run_id] = {"session_id": result["session_id"], "repo": repo,
                        "interrupt_id": result["pending"]["interrupt_id"]}
    else:
        runs.pop(run_id, None)
    tool_context.state[_STATE_KEY] = runs


def start_investigation(repo: str, run_id: str, tool_context: ToolContext) -> dict:
    """Start a NEW e2e-agent investigation of one failed GitHub Actions run.

    Use this only when no prior investigation exists, or the user explicitly
    wants a fresh one. History and trends belong to ask_data_agent, not here.

    Args:
      repo: GitHub repo as "owner/repo".
      run_id: The failed workflow run id.

    Returns:
      {status, report, pending}. status is "done" (report is final),
      "needs_input" (e2e-agent paused for a human; report ends with the
      finish/dig_deeper question - show it verbatim, then call
      resume_investigation with the HUMAN's answer), or "failed".
    """
    if not PROJECT_ID:
        return _failed("GOOGLE_CLOUD_PROJECT is not set.")
    try:
        result = e2e_edge.start(_engine(), user_id=_user_id(repo, run_id),
                                repo=repo, run_id=run_id)
    except RuntimeError as exc:
        return _failed(str(exc))
    _record_handle(tool_context, repo, run_id, result)
    return {k: result[k] for k in ("status", "report", "pending")}


def resume_investigation(run_id: str, decision: str, focus: str,
                         tool_context: ToolContext) -> dict:
    """Answer a paused investigation with what the human chose.

    Only call this after the human has said finish or dig_deeper in their own
    words. Never choose for them.

    Args:
      run_id: The run whose investigation is paused (needs_input).
      decision: "finish" or "dig_deeper".
      focus: For dig_deeper, the path, symbol, or question to look at. Empty
        for finish.

    Returns:
      Same shape as start_investigation. Can pause again (needs_input).
    """
    if decision not in ("finish", "dig_deeper"):
        return _failed(f"decision must be finish or dig_deeper, got {decision!r}")
    if decision == "dig_deeper" and not focus.strip():
        return _failed("dig_deeper needs a focus; ask the human what to look at.")
    handle = (tool_context.state.get(_STATE_KEY) or {}).get(run_id)
    if not handle:
        return _failed(f"no paused investigation for run {run_id} in this session")
    if not PROJECT_ID:
        return _failed("GOOGLE_CLOUD_PROJECT is not set.")
    try:
        result = e2e_edge.resume(
            _engine(), user_id=_user_id(handle["repo"], run_id),
            session_id=handle["session_id"], interrupt_id=handle["interrupt_id"],
            decision=decision, focus=focus.strip())
    except RuntimeError as exc:
        return _failed(str(exc))
    _record_handle(tool_context, handle["repo"], run_id, result)
    return {k: result[k] for k in ("status", "report", "pending")}


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

2. "Investigate this failure" / "why did run <id> fail":
   a. First ask `ask_data_agent` whether run <id> was already investigated.
      If a prior report exists, return it and offer a fresh investigation.
   b. Otherwise call `start_investigation` with the repo and run_id.
   c. status "done": relay the report. status "failed": say so plainly.
   d. status "needs_input": show the report and question exactly as returned.
      Do NOT choose finish or dig_deeper yourself. When the human answers,
      call `resume_investigation` with their decision (and focus for
      dig_deeper). It may pause again; repeat.
""",
    tools=[_data_agent_toolset, FunctionTool(start_investigation),
           FunctionTool(resume_investigation)],
)

app = App(name="sre_coordinator", root_agent=root_agent)
