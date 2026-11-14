"""Attach to a paused e2e-agent session from an MCP host (Claude Code).

e2e-agent runs that were started elsewhere (the coordinator, an automation that
posted to Slack) stop at dormancy_gate with a RequestInput. A paused session is
not running: the :streamQuery stream ended at the pause, so it is only events
in the session service. This server reads those events and, on request, answers
the pause with a new stream_query on the same session.

The only coordinates a developer needs are `repo` and `run_id`. The engine
enforces user_id on every session call, and e2e_edge.user_id_for derives it
from those two, the same way the coordinator files the session.

Wire-format parsing is agent_serving.e2e_edge, shared with the coordinator.
Runs as the developer: Application Default Credentials need
roles/aiplatform.user on the e2e-agent engine.

    claude mcp add e2e-session -- .venv/bin/python deploy/session_mcp.py

Accepted limitations:
  - Two people answering the same pause: the loser gets an error (the
    interrupt is already consumed). Not guarded further at this scale.
  - A dig_deeper answer holds a stream_query open for minutes, and a dropped
    stream cancels the run (see docs/geap-adk-streamquery.md). The stream is
    held by a thread in THIS process, so closing the MCP host mid-run kills it.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

import agentplatform
from agent_serving import e2e_edge

# stream_query inherits the google-genai client's HTTP timeout, which is unset
# (waits as long as the server streams). The bound that matters is this one: a
# run still going after it is reported by session_status as past its deadline,
# so a hung stream is visible. It cannot be cancelled from here (a blocked
# iterator has no cancel); the daemon thread dies with the process.
RUN_DEADLINE_S = float(os.environ.get("SESSION_MCP_RUN_DEADLINE_S", "1800"))

_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()
_engine_cache: list[Any] = []


def _load_dotenv() -> None:
    env = Path(__file__).resolve().parents[1] / ".env"
    if not env.is_file():
        return
    for line in env.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _engine() -> Any:
    if _engine_cache:
        return _engine_cache[0]
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    if not project:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT is not set.")
    client = agentplatform.Client(
        project=project, location=os.environ.get("AGENT_LOCATION", "us-central1"))
    name = os.environ.get("E2E_AGENT_ENGINE", "").strip()
    if not name:
        # runtimes.list() pages are bounded by the API's page size; a project
        # has a handful of engines.
        for runtime in client.runtimes.list():
            api = getattr(runtime, "api_resource", runtime)
            if getattr(api, "display_name", None) == "e2e-agent":
                name = api.name
                break
    if not name:
        raise RuntimeError("no Agent Runtime named e2e-agent; set E2E_AGENT_ENGINE")
    _engine_cache.append(client.runtimes.get(name=name))
    return _engine_cache[0]


def _job_view(job: dict[str, Any]) -> dict[str, Any]:
    elapsed = time.monotonic() - job["started"]
    if job["thread"].is_alive():
        state = "running_past_deadline" if elapsed > RUN_DEADLINE_S else "running"
        return {"state": state, "elapsed_s": round(elapsed)}
    if job.get("error"):
        return {"state": "answer_failed", "error": job["error"]}
    result = job["result"]
    return {"state": {"needs_input": "waiting", "done": "done", "failed": "failed"}[result["status"]],
            "report": result["report"], "interrupt_id": (result["pending"] or {}).get("interrupt_id")}


def _run(job: dict[str, Any], **kwargs: Any) -> None:
    try:
        job["result"] = e2e_edge.resume(_engine(), **kwargs)
    except Exception as exc:  # surfaced through session_status, never swallowed
        job["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"


server = MCPServer(
    name="e2e_session",
    instructions=(
        "Read and answer paused e2e-agent investigations by (repo, run_id). "
        "Call session_status first and show its report verbatim. Only call "
        "session_answer with what the human chose."
    ),
)


@server.tool()
def session_status(repo: str, run_id: str) -> dict[str, Any]:
    """What the e2e-agent investigation of this run is doing right now.

    Args:
      repo: "owner/repo", as the investigation was started with.
      run_id: The failed workflow run id.

    Returns {state, report, interrupt_id, session_id}. state is "waiting"
    (paused for a human; report is the finding plus the finish/dig_deeper
    question), "running" (an answer from this server is in flight),
    "running_past_deadline", "answer_failed", "done", "failed", or
    "not_found" (no session filed under this repo and run_id).
    """
    user_id = e2e_edge.user_id_for(repo, run_id)
    with _lock:
        job = _jobs.get(user_id)
    if job:
        return {**_job_view(job), "session_id": job["session_id"]}
    engine = _engine()
    sid = e2e_edge.latest_session_id(engine, user_id=user_id)
    if sid is None:
        return {"state": "not_found", "session_id": None}
    pending = e2e_edge.pending_interrupt(engine, user_id=user_id, session_id=sid)
    if pending is None:
        return {"state": "done", "session_id": sid, "report": "", "interrupt_id": None}
    return {"state": "waiting", "session_id": sid, "interrupt_id": pending["interrupt_id"],
            "report": pending["message"][: e2e_edge.REPORT_CAP]}


@server.tool()
def session_answer(repo: str, run_id: str, decision: str, focus: str = "") -> dict[str, Any]:
    """Answer the pause of a waiting investigation, then return immediately.

    The investigation continues in the background of this server. Poll
    session_status for the outcome: a dig_deeper takes minutes and pauses
    again with a new report. Closing the MCP host cancels the run.

    Args:
      repo: "owner/repo".
      run_id: The failed workflow run id.
      decision: "finish" closes the investigation; "dig_deeper" runs one more pass.
      focus: Required for dig_deeper: a path, symbol or question to look at.
    """
    if decision not in ("finish", "dig_deeper"):
        return {"state": "rejected", "error": "decision must be finish or dig_deeper"}
    if decision == "dig_deeper" and not focus.strip():
        # The graph would reject this and burn a dormancy retry; refuse here.
        return {"state": "rejected", "error": "dig_deeper requires a focus"}
    user_id = e2e_edge.user_id_for(repo, run_id)
    with _lock:
        running = _jobs.get(user_id)
        if running and running["thread"].is_alive():
            return {"state": "rejected", "error": "an answer is already in flight"}
        engine = _engine()
        sid = e2e_edge.latest_session_id(engine, user_id=user_id)
        if sid is None:
            return {"state": "not_found", "session_id": None}
        pending = e2e_edge.pending_interrupt(engine, user_id=user_id, session_id=sid)
        if pending is None:
            return {"state": "rejected", "session_id": sid,
                    "error": "the session is not waiting for input"}
        job: dict[str, Any] = {"session_id": sid, "started": time.monotonic()}
        job["thread"] = threading.Thread(
            target=_run, daemon=True, args=(job,),
            kwargs=dict(user_id=user_id, session_id=sid,
                        interrupt_id=pending["interrupt_id"],
                        decision=decision, focus=focus.strip()))
        _jobs[user_id] = job
        job["thread"].start()
    return {"state": "started", "session_id": sid, "interrupt_id": pending["interrupt_id"]}


if __name__ == "__main__":
    _load_dotenv()
    server.run(transport="stdio")
