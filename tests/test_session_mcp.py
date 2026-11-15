"""deploy/session_mcp.py: attach to a paused session. Offline, strict fake engine."""

import importlib.util
import threading
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "session_mcp", Path(__file__).parents[1] / "deploy" / "session_mcp.py")
m = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(m)

REPO, RUN = "o/r", "1"
UID = "sre-coordinator:o/r:1"


def _pause(iid="deep_dive_request_0"):
    return {"content": {"parts": [{"functionCall": {
        "name": "adk_request_input", "id": iid, "args": {"message": "Choose"}}}]}}


class Engine:
    """Knows one user_id, like the live engine; every other one is an error."""

    def __init__(self, events, gate=None):
        self.events, self.gate, self.queries = events, gate, []

    def _check(self, user_id):
        assert user_id == UID, "wrong user_id"

    def list_sessions(self, *, user_id):
        return {"sessions": [{"id": "s1", "lastUpdateTime": 1.0}]} if user_id == UID else {"sessions": []}

    def get_session(self, *, user_id, session_id):
        self._check(user_id)
        return {"id": session_id, "events": list(self.events)}

    def stream_query(self, *, user_id, message, session_id=None):
        self._check(user_id)
        self.queries.append(message)
        if self.gate:
            self.gate.wait(5)
        yield {"content": {"parts": [{"text": "final report"}]}}


@pytest.fixture(autouse=True)
def _clean():
    m._jobs.clear()
    m._engine_cache.clear()
    yield
    m._jobs.clear()
    m._engine_cache.clear()


def _use(engine):
    m._engine_cache.append(engine)


def test_status_waiting_not_found_and_done():
    _use(Engine([_pause()]))
    r = m.session_status(REPO, RUN)
    assert r["state"] == "waiting" and r["interrupt_id"] == "deep_dive_request_0"
    assert m.session_status(REPO, "other")["state"] == "not_found"
    m._engine_cache[0].events = [{"content": {"parts": [{"text": "x"}]}}]
    assert m.session_status(REPO, RUN)["state"] == "done"


def test_dig_deeper_without_focus_is_refused_before_any_engine_call():
    eng = Engine([_pause()])
    _use(eng)
    assert m.session_answer(REPO, RUN, "dig_deeper", "  ")["state"] == "rejected"
    assert m.session_answer(REPO, RUN, "maybe")["state"] == "rejected"
    assert eng.queries == []


def test_answer_when_not_waiting_is_refused():
    _use(Engine([]))
    r = m.session_answer(REPO, RUN, "finish")
    assert r["state"] == "rejected" and "not waiting" in r["error"]


def test_answer_runs_in_background_and_a_second_is_refused_meanwhile():
    gate = threading.Event()
    eng = Engine([_pause()], gate=gate)
    _use(eng)
    assert m.session_answer(REPO, RUN, "finish")["state"] == "started"
    assert m.session_status(REPO, RUN)["state"] == "running"
    assert m.session_answer(REPO, RUN, "finish")["state"] == "rejected"
    gate.set()
    m._jobs[UID]["thread"].join(5)
    r = m.session_status(REPO, RUN)
    assert r["state"] == "done" and r["report"] == "final report"
    fr = eng.queries[0]["parts"][0]["function_response"]
    assert fr["id"] == "deep_dive_request_0" and fr["response"] == {"decision": "finish", "focus": ""}


def test_a_failed_answer_is_visible_not_swallowed():
    class Boom(Engine):
        def stream_query(self, **kw):
            raise RuntimeError("engine said no")
    _use(Boom([_pause()]))
    m.session_answer(REPO, RUN, "finish")
    m._jobs[UID]["thread"].join(5)
    r = m.session_status(REPO, RUN)
    assert r["state"] == "answer_failed" and "engine said no" in r["error"]


def test_a_run_past_its_deadline_says_so(monkeypatch):
    gate = threading.Event()
    _use(Engine([_pause()], gate=gate))
    m.session_answer(REPO, RUN, "finish")
    monkeypatch.setattr(m, "RUN_DEADLINE_S", -1)
    assert m.session_status(REPO, RUN)["state"] == "running_past_deadline"
    gate.set()
    m._jobs[UID]["thread"].join(5)
