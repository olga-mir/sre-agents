"""The sre_coordinator -> e2e-agent edge, as a resumable call.

e2e-agent can return without being done: dormancy_gate yields a RequestInput
and the run pauses until a human answers. A plain "call, get a string" tool
cannot express that, so the edge returns an `E2EResult` whose `status` says
which of three things happened, and whose `pending` carries what a resume
needs. No import-time side effects here, so tests load it without GCP.

Wire shapes, from google-adk (workflow/utils/_workflow_hitl_utils.py):
  - the pause is an event with a function call named `adk_request_input`
    whose `id` is the RequestInput `interrupt_id`;
  - the reply is a function response with that same id and name.
Agent Runtime serialises events to JSON dicts, so the keys may be camelCase
(`functionCall`) or snake_case; both are read. NOT yet confirmed against a
live engine: that `stream_query(message=<Content dict>)` is accepted for a
resume, and that the engine client exposes `get_session(user_id, session_id)`
returning the session's events (used by `pending_interrupt`). Spike both first (see docs in the PR description).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Literal, Protocol, TypedDict

from google.genai.errors import UnknownApiResponseError

# Cap on report text handed back to the coordinator LLM. It bounds the
# coordinator's context, not the investigation.
REPORT_CAP = 8000

# Cap on events consumed from one stream_query. Without it a runaway remote
# loop streams forever into this tool call. The e2e-agent's own budgets
# (AGENTS.md invariants 1-4) are the real bound; this is the caller's backstop.
MAX_EVENTS = 2000

REQUEST_INPUT_CALL = "adk_request_input"

Status = Literal["done", "needs_input", "failed"]


class Pending(TypedDict):
    interrupt_id: str
    message: str


class E2EResult(TypedDict):
    status: Status
    report: str
    pending: Pending | None
    session_id: str | None


class Engine(Protocol):
    """The slice of the Agent Runtime client this edge uses."""

    def create_session(self, *, user_id: str) -> Any: ...

    def stream_query(
        self, *, user_id: str, message: Any, session_id: str | None = None
    ) -> Iterable[Any]: ...

    def get_session(self, *, user_id: str, session_id: str) -> Any:
        """The remote session as a dict (or object) carrying `events`."""


@dataclass
class _Scan:
    texts: list[str]
    pending: Pending | None


def _get(d: dict, *keys: str) -> Any:
    for k in keys:
        if k in d:
            return d[k]
    return None


_RAW_MARKER = "Raw response: "


def _salvage(exc: UnknownApiResponseError) -> list[dict]:
    """Decode the events out of a chunk the SDK failed to split.

    google-genai finds event boundaries by counting `{` and `}` per line
    without regard for JSON strings. A CI log line with an unbalanced brace
    (a tool response quoting a log) leaves the count off for the rest of the
    stream, so every later event, the final report and the request-input
    pause included, arrives as one concatenated chunk and json.loads raises
    "Extra data". The whole chunk is in the exception text; decode it object by
    object. Bounded by the length of that text.
    """
    msg = str(exc)
    _, found, raw = msg.partition(_RAW_MARKER)
    if not found:
        return []
    decoder = json.JSONDecoder()
    events: list[dict] = []
    i = 0
    while i < len(raw):
        while i < len(raw) and raw[i].isspace():
            i += 1
        if i >= len(raw):
            break
        try:
            obj, i = decoder.raw_decode(raw, i)
        except ValueError:
            break
        if isinstance(obj, dict):
            events.append(obj)
    return events


def _events(stream: Iterable[Any]) -> Iterator[Any]:
    """Yield stream events, recovering from the SDK's chunk-splitting failure.

    The SDK generator is dead after it raises, but the failing chunk is the
    last one it would have produced (an unbalanced count never returns to
    zero, so the remainder is flushed as a single chunk), so nothing after it
    is lost.
    """
    try:
        yield from stream
    except UnknownApiResponseError as exc:
        events = _salvage(exc)
        if not events:
            raise RuntimeError(
                f"e2e-agent stream was unparseable and nothing was recoverable: "
                f"{str(exc)[:300]}") from exc
        yield from events


def _scan(events: Iterable[Any]) -> _Scan:
    """Collect authored text and the request-input pause, if any.

    Only text parts and the request-input call are kept. Other function-call
    and function-response parts can carry a single tool's whole log dump and
    crashed the google-genai client when fed back as this tool's response.
    """
    texts: list[str] = []
    pending: Pending | None = None
    for n, event in enumerate(_events(events)):
        if n >= MAX_EVENTS:
            break
        if not isinstance(event, dict):
            continue
        for part in (event.get("content") or {}).get("parts") or []:
            call = _get(part, "functionCall", "function_call")
            if call and call.get("name") == REQUEST_INPUT_CALL and call.get("id"):
                args = call.get("args") or {}
                # Last pause wins: a re-prompt supersedes the earlier one.
                pending = Pending(
                    interrupt_id=call["id"],
                    message=str(args.get("message") or ""),
                )
                continue
            if part.get("text"):
                texts.append(part["text"])
    return _Scan(texts=texts, pending=pending)


def _result(session_id: str | None, scan: _Scan) -> E2EResult:
    report = "\n".join(scan.texts).strip()
    if scan.pending:
        # The pause message is the report plus the finish/dig_deeper question;
        # keep it as the report when no separate text arrived.
        report = report or scan.pending["message"]
        return E2EResult(status="needs_input", report=report[:REPORT_CAP],
                         pending=scan.pending, session_id=session_id)
    if not report:
        return E2EResult(
            status="failed",
            report="e2e-agent emitted no text - check its own session trace.",
            pending=None, session_id=session_id)
    return E2EResult(status="done", report=report[:REPORT_CAP],
                     pending=None, session_id=session_id)


def _session_id(session: Any) -> str:
    sid = session.get("id") if isinstance(session, dict) else getattr(session, "id", None)
    if not sid:
        raise RuntimeError(f"create_session returned no id: {session!r}")
    return str(sid)


def start(engine: Engine, *, user_id: str, repo: str, run_id: str) -> E2EResult:
    """Open a remote session and run the investigation until it ends or pauses."""
    # trigger_source only accepts TEST_OPERATOR or SLACK_MANUAL
    # (agents/e2e-agent/e2e_agent/agent/state.py); a coordinator-triggered single-run
    # investigation has the shape of TEST_OPERATOR's.
    payload = json.dumps({"trigger_source": "TEST_OPERATOR",
                          "repo": repo, "run_id": run_id})
    sid = _session_id(engine.create_session(user_id=user_id))
    scan = _scan(engine.stream_query(user_id=user_id, session_id=sid, message=payload))
    return _result(sid, scan)


def pending_interrupt(engine: Engine, *, user_id: str, session_id: str) -> Pending | None:
    """The request-input pause the remote session is waiting on right now.

    A pause is open when its call id has no later function response with the
    same id. Last open pause wins, as in `_scan`. None means the session is not
    waiting on anyone.
    """
    session = engine.get_session(user_id=user_id, session_id=session_id)
    events = session.get("events") if isinstance(session, dict) else getattr(session, "events", None)
    open_: dict[str, Pending] = {}
    for n, event in enumerate(events or []):
        if n >= MAX_EVENTS:
            break
        if not isinstance(event, dict):
            continue
        for part in (event.get("content") or {}).get("parts") or []:
            call = _get(part, "functionCall", "function_call")
            if call and call.get("name") == REQUEST_INPUT_CALL and call.get("id"):
                open_[call["id"]] = Pending(
                    interrupt_id=call["id"],
                    message=str((call.get("args") or {}).get("message") or ""))
            resp = _get(part, "functionResponse", "function_response")
            if resp and resp.get("name") == REQUEST_INPUT_CALL:
                open_.pop(resp.get("id"), None)
    return list(open_.values())[-1] if open_ else None


def resume(engine: Engine, *, user_id: str, session_id: str, interrupt_id: str,
           decision: Literal["finish", "dig_deeper"], focus: str = "") -> E2EResult:
    """Answer a paused investigation with the human's DeepDiveFeedback.

    The stored interrupt_id can be stale: an earlier resume may have timed out
    after the remote node consumed the answer. So the remote session is asked
    what it is waiting on first, and the answer is sent only if that is the
    interrupt the human was shown.
    """
    pending = pending_interrupt(engine, user_id=user_id, session_id=session_id)
    if pending is None:
        return E2EResult(
            status="failed", pending=None, session_id=session_id,
            report="e2e-agent is not waiting for input; an earlier answer was "
                   "probably already consumed. Nothing was sent. Check the "
                   "investigation history instead of resuming.")
    if pending["interrupt_id"] != interrupt_id:
        # The human never saw this prompt; answering it blindly would be
        # choosing for them. Surface it and let them answer.
        return E2EResult(status="needs_input", session_id=session_id,
                         report=pending["message"][:REPORT_CAP], pending=pending)
    message = {
        "role": "user",
        "parts": [{
            "function_response": {
                "id": interrupt_id,
                "name": REQUEST_INPUT_CALL,
                "response": {"decision": decision, "focus": focus},
            },
        }],
    }
    scan = _scan(engine.stream_query(user_id=user_id, session_id=session_id,
                                     message=message))
    return _result(session_id, scan)
