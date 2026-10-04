"""Drive the graph locally: investigate a real run, pause for a reply, resume.

Deployment to GEAP is a separate step; this is the loop that proves the graph
works against live GitHub and a live model first.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from google.adk.runners import InMemoryRunner
from google.genai import types

from .agent import app
from .nodes import render_report
from .state import InvestigationSummary, TriggerEvent

APP_NAME = "ci_agent"
USER_ID = "local"
_FINISH_WORDS = frozenset({"no", "n", "finish", "done", "stop"})


def _hitl_response(feedback: str) -> dict[str, str]:
    """Same two fields the Gemini Enterprise form submits."""
    text = feedback.strip()
    if text.lower() in _FINISH_WORDS:
        return {"decision": "finish", "focus": ""}
    return {"decision": "dig_deeper", "focus": text}


def _node_name(event: Any) -> Optional[str]:
    return getattr(getattr(event, "actions", None), "node_name", None) \
        or getattr(event, "author", None)


def _describe(event: Any) -> Optional[str]:
    calls = event.get_function_calls() if hasattr(event, "get_function_calls") else []
    if calls:
        return " ".join(
            f"-> {c.name}({json.dumps(c.args, default=str)[:120]})" for c in calls)
    if getattr(event, "output", None) is not None:
        out = event.output
        text = out if isinstance(out, str) else json.dumps(out, default=str)
        return f"<= {text[:300]}"
    return None


def _trace_row(event: Any) -> dict[str, Any]:
    """One line of the durable trace. Tool ARGUMENTS are recorded, tool results
    are not - the results are the evidence, and they are dumped separately."""
    return {
        "node": _node_name(event),
        "calls": [{"name": c.name, "args": c.args}
                  for c in (event.get_function_calls() or [])],
        "output": event.output if getattr(event, "output", None) is not None else None,
    }


async def _dump(runner: Any, session_id: str, out_dir: Path,
                trace: list[dict[str, Any]], summaries: dict[str, Any]) -> None:
    """Write everything a postmortem needs: the trace, the findings, the report,
    and the evidence bytes the run actually read."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "trace.jsonl").write_text(
        "\n".join(json.dumps(row, default=str) for row in trace) + "\n")
    (out_dir / "summaries.json").write_text(json.dumps(summaries, indent=2,
                                                       default=str))
    reports = [render_report(InvestigationSummary(**s)) for s in summaries.values()]
    (out_dir / "report.md").write_text("\n\n---\n\n".join(reports) + "\n")

    service = runner.artifact_service
    keys = await service.list_artifact_keys(
        app_name=APP_NAME, user_id=USER_ID, session_id=session_id)
    for key in keys:
        part = await service.load_artifact(
            app_name=APP_NAME, user_id=USER_ID, session_id=session_id, filename=key)
        blob = getattr(part, "inline_data", None)
        data = getattr(blob, "data", None) if blob is not None else None
        if data is None:
            continue
        # `key` is already a path (ev/{run}/{kind}/{sha12}/{path}), so the dump
        # is greppable by path at 3am without joining against session state.
        target = out_dir / "evidence" / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


async def investigate(repo: str, run_id: str, *, verbose: bool = True,
                      feedback: Optional[str] = None,
                      out_dir: Optional[str] = None) -> dict[str, Any]:
    """Run one investigation. If `feedback` is given, answer the HITL prompt
    with it and let the deep dive run; otherwise stop at the pause.

    With `out_dir`, the trace, findings, report and evidence bytes are written
    there - InMemoryRunner keeps artifacts in memory, so without this the
    evidence a run read disappears with the process.
    """
    runner = InMemoryRunner(app=app)
    session = await runner.session_service.create_session(
        app_name=APP_NAME, user_id=USER_ID)

    trigger = TriggerEvent(repo=repo, run_id=run_id)
    message = types.Content(role="user",
                            parts=[types.Part(text=trigger.model_dump_json())])

    interrupt = None
    trace: list[dict[str, Any]] = []
    async for event in runner.run_async(user_id=USER_ID, session_id=session.id,
                                        new_message=message):
        trace.append(_trace_row(event))
        if verbose and (line := _describe(event)):
            print(f"[{_node_name(event)}] {line}")
        for call in (event.get_function_calls() or []):
            if call.name == "adk_request_input":
                interrupt = call

    result: dict[str, Any] = {"session_id": session.id, "paused": interrupt is not None}
    if interrupt is not None and feedback is not None:
        reply = types.Content(role="user", parts=[types.Part(
            function_response=types.FunctionResponse(
                id=interrupt.id, name=interrupt.name,
                response=_hitl_response(feedback)))])
        async for event in runner.run_async(user_id=USER_ID, session_id=session.id,
                                            new_message=reply):
            trace.append(_trace_row(event))
            if verbose and (line := _describe(event)):
                print(f"[{_node_name(event)}] {line}")
        result["paused"] = False

    after = await runner.session_service.get_session(
        app_name=APP_NAME, user_id=USER_ID, session_id=session.id)
    result["summaries"] = (after.state or {}).get("investigation_summaries", {})
    if out_dir:
        path = Path(out_dir) / run_id
        await _dump(runner, session.id, path, trace, result["summaries"])
        from ..analytics.collect import write_jsonl
        from ..analytics.investigation_rows import rows_from_dump
        mapped = rows_from_dump(
            path, repo=repo, session_id=session.id, paused=result["paused"])
        analytics_dir = path / "analytics"
        for table, rows in mapped.items():
            write_jsonl(analytics_dir / f"{table}.jsonl", rows)
        result["out_dir"] = str(path)
    return result
