"""The coordinator -> e2e-agent edge. Offline: a strict fake engine, no GCP.

agents/sre_coordinator/__init__ imports agent.py, which builds GCP clients at
import time, so the pure module is loaded by path instead.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "e2e_edge", Path(__file__).parents[1] / "agents/sre_coordinator/e2e_edge.py")
edge = importlib.util.module_from_spec(_SPEC)
sys.modules["e2e_edge"] = edge  # @dataclass resolves annotations via sys.modules
_SPEC.loader.exec_module(edge)


def text(t):
    return {"content": {"parts": [{"text": t}]}}


def pause(interrupt_id="deep_dive_request_0", message="Choose finish or dig_deeper"):
    return {"content": {"parts": [{"functionCall": {
        "name": "adk_request_input", "id": interrupt_id,
        "args": {"message": message}}}]}}


class FakeEngine:
    """Rejects unknown kwargs and unknown sessions, like the real client."""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.sessions = set()
        self.queries = []
        self.events = []

    def create_session(self, *, user_id):
        self.sessions.add("s1")
        return {"id": "s1"}

    def stream_query(self, *, user_id, message, session_id=None):
        assert session_id in self.sessions, "resume/start needs a created session"
        self.queries.append((session_id, message))
        script = self.scripts.pop(0)
        if isinstance(message, dict):
            fr = message["parts"][0]["function_response"]
            self.events.append({"content": {"parts": [{"functionResponse": fr}]}})
        return self._record(script)

    def _record(self, script):
        for event in script:  # lazy: a runaway stream must not be drained here
            self.events.append(event)
            yield event

    def get_session(self, *, user_id, session_id):
        assert session_id in self.sessions, "unknown session"
        return {"id": session_id, "events": list(self.events)}


def test_start_done():
    eng = FakeEngine([[text("root cause: X"), text("verified")]])
    r = edge.start(eng, user_id="u", repo="o/r", run_id="1")
    assert r["status"] == "done" and r["pending"] is None
    assert r["report"] == "root cause: X\nverified"


def test_start_pause_carries_resume_handle():
    eng = FakeEngine([[text("report body"), pause()]])
    r = edge.start(eng, user_id="u", repo="o/r", run_id="1")
    assert r["status"] == "needs_input"
    assert r["pending"]["interrupt_id"] == "deep_dive_request_0"
    assert r["session_id"] == "s1"
    assert "report body" in r["report"]


def test_pause_message_used_when_no_text():
    eng = FakeEngine([[pause(message="only the question")]])
    r = edge.start(eng, user_id="u", repo="o/r", run_id="1")
    assert r["report"] == "only the question"


def test_snake_case_keys_also_read():
    ev = {"content": {"parts": [{"function_call": {
        "name": "adk_request_input", "id": "i1", "args": {}}}]}}
    r = edge.start(FakeEngine([[ev]]), user_id="u", repo="o/r", run_id="1")
    assert r["status"] == "needs_input"


def test_other_function_parts_dropped():
    big = {"content": {"parts": [{"functionResponse": {"response": "x" * 100000}}]}}
    r = edge.start(FakeEngine([[big, text("ok")]]), user_id="u", repo="o/r", run_id="1")
    assert r["report"] == "ok"


def test_no_text_is_failed_not_done():
    r = edge.start(FakeEngine([[]]), user_id="u", repo="o/r", run_id="1")
    assert r["status"] == "failed"


def test_report_capped():
    r = edge.start(FakeEngine([[text("a" * 20000)]]), user_id="u", repo="o/r", run_id="1")
    assert len(r["report"]) == edge.REPORT_CAP


def test_event_count_bounded():
    many = (text("x") for _ in range(edge.MAX_EVENTS * 10))
    r = edge.start(FakeEngine([many]), user_id="u", repo="o/r", run_id="1")
    assert r["report"].count("x") == edge.MAX_EVENTS


def test_resume_sends_function_response_with_matching_id():
    eng = FakeEngine([[pause()], [text("final"), ]])
    edge.start(eng, user_id="u", repo="o/r", run_id="1")
    r = edge.resume(eng, user_id="u", session_id="s1",
                    interrupt_id="deep_dive_request_0",
                    decision="dig_deeper", focus="proxy.go")
    assert r["status"] == "done"
    sid, msg = eng.queries[-1]
    fr = msg["parts"][0]["function_response"]
    assert sid == "s1"
    assert fr == {"id": "deep_dive_request_0", "name": "adk_request_input",
                  "response": {"decision": "dig_deeper", "focus": "proxy.go"}}


def test_resume_can_pause_again_with_new_interrupt_id():
    eng = FakeEngine([[pause("deep_dive_request_0")],
                      [text("pass 2"), pause("deep_dive_request_1")]])
    edge.start(eng, user_id="u", repo="o/r", run_id="1")
    r = edge.resume(eng, user_id="u", session_id="s1",
                    interrupt_id="deep_dive_request_0",
                    decision="dig_deeper", focus="x")
    assert r["status"] == "needs_input"
    assert r["pending"]["interrupt_id"] == "deep_dive_request_1"


def test_create_session_without_id_raises():
    class Bad(FakeEngine):
        def create_session(self, *, user_id):
            return {}
    with pytest.raises(RuntimeError):
        edge.start(Bad([[]]), user_id="u", repo="o/r", run_id="1")


def test_resume_with_consumed_interrupt_sends_nothing():
    # A first resume timed out client-side after the remote consumed the answer;
    # the retry replays the same interrupt_id.
    eng = FakeEngine([[pause("i0")], [text("final")]])
    edge.start(eng, user_id="u", repo="o/r", run_id="1")
    edge.resume(eng, user_id="u", session_id="s1", interrupt_id="i0", decision="finish")
    sent = len(eng.queries)
    r = edge.resume(eng, user_id="u", session_id="s1", interrupt_id="i0", decision="finish")
    assert r["status"] == "failed" and "not waiting" in r["report"]
    assert len(eng.queries) == sent


def test_resume_with_stale_id_surfaces_new_prompt_instead_of_answering():
    eng = FakeEngine([[pause("i0")], [text("pass 2"), pause("i1", "second question")]])
    edge.start(eng, user_id="u", repo="o/r", run_id="1")
    # consumed by the remote, but the caller never saw the reply
    edge.resume(eng, user_id="u", session_id="s1", interrupt_id="i0",
                decision="dig_deeper", focus="x")
    sent = len(eng.queries)
    r = edge.resume(eng, user_id="u", session_id="s1", interrupt_id="i0", decision="finish")
    assert r["status"] == "needs_input"
    assert r["pending"]["interrupt_id"] == "i1" and r["report"] == "second question"
    assert len(eng.queries) == sent


def _mischunked(*events, head=()):
    """A stream that yields `head`, then raises as google-genai does when its
    brace counter loses sync: one chunk holding every remaining event."""
    import json
    from google.genai.errors import UnknownApiResponseError

    def gen():
        yield from head
        raw = "\n".join(json.dumps(e) for e in events)
        raise UnknownApiResponseError(
            f"Failed to parse response as JSON. Raw response: {raw}")
    return gen()


class _StreamEngine(FakeEngine):
    def __init__(self, stream):
        super().__init__([])
        self._stream = stream

    def stream_query(self, *, user_id, message, session_id=None):
        assert session_id in self.sessions
        return self._stream


def test_mischunked_stream_is_recovered_with_report_and_pause():
    # The unbalanced-brace log line in a tool response is what desyncs the SDK.
    tool = {"content": {"parts": [{"function_response": {
        "name": "find_in_log", "response": {"line": "panic: {unclosed"}}}]}}
    stream = _mischunked(tool, text("root cause: X"), pause(), head=[text("first")])
    r = edge.start(_StreamEngine(stream), user_id="u", repo="o/r", run_id="1")
    assert r["status"] == "needs_input"
    assert r["pending"]["interrupt_id"] == "deep_dive_request_0"
    assert r["report"] == "first\nroot cause: X"


def test_unrecoverable_stream_error_is_runtime_error_not_a_crash():
    from google.genai.errors import UnknownApiResponseError

    def gen():
        raise UnknownApiResponseError("Failed to parse response as JSON. Raw response: <html>")
        yield
    with pytest.raises(RuntimeError, match="nothing was recoverable"):
        edge.start(_StreamEngine(gen()), user_id="u", repo="o/r", run_id="1")
