"""Node-level tests. No network, no model: fakes for Context and GitHub."""

import pytest

from e2e_agent.agent import nodes
from e2e_agent.agent.nodes import _banner, branch_guard, render_report, trigger_router
from e2e_agent.agent.state import (
    CodeFinding, DeepDiveFeedback, EvidenceRef, InvestigationSummary,
    TriggerEvent, merge_summary,
)
from e2e_agent.store import evidence_filename

HEAD = "h" * 40
BASE = "b" * 40

RUN_CONTEXT = {
    "repo": "o/r", "run_id": "99", "head_sha": HEAD, "base_sha": BASE,
    "base_ref_name": "main", "module": "github.com/o/r", "pr_number": 7,
    "jobs": [{"job_id": 5, "name": "run-tests", "conclusion": "failure"}],
}


class FakePart:
    def __init__(self, data): self.inline_data = type("B", (), {"data": data})()


class FakeCtx:
    def __init__(self, artifacts=None, state=None, resume_inputs=None):
        self.artifacts = artifacts or {}
        self.state = state or {"run_context": RUN_CONTEXT}
        self.resume_inputs = resume_inputs

    async def load_artifact(self, filename, version=None):
        data = self.artifacts.get(filename)
        return FakePart(data) if data is not None else None

    async def save_artifact(self, filename, artifact, custom_metadata=None):
        return 1


class FakeGitHub:
    """Stands in for the verifier's manifest read."""

    changed = ["pkg/real.go"]

    def __init__(self, repo): self.repo = repo

    async def compare_files(self, base_sha, head_sha):
        from e2e_agent.github.client import ChangedFile
        return [ChangedFile(path=p, status="modified", additions=1, deletions=0)
                for p in self.changed]

    async def aclose(self): pass


async def verify(finding: CodeFinding, ctx: FakeCtx) -> InvestigationSummary:
    summary = InvestigationSummary(
        failure_id="primary", confidence=finding.confidence,
        one_line_summary=finding.one_line_summary, finding=finding)
    events = [e async for e in nodes.verifier_linter(summary.model_dump(), ctx)]
    return InvestigationSummary(**events[-1].output)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(nodes, "GitHub", FakeGitHub)


def test_investigator_and_deep_dive_include_thoughts():
    for agent in (nodes.investigator_agent, nodes.deep_dive_agent):
        assert agent.planner.thinking_config.include_thoughts is True


def test_investigator_instruction_asks_for_parallel_discovery():
    text = nodes.INVESTIGATOR_INSTRUCTION
    assert "same turn" in text
    assert "Do not call\n`list_failed_jobs` first" in text


def test_trigger_router_accepts_both_valid_sources():
    for source in ("TEST_OPERATOR", "SLACK_MANUAL"):
        event = trigger_router(TriggerEvent(trigger_source=source, repo="o/r",
                                            run_id="1"))
        # One shared route: two labelled edges into pin_run_context would be
        # rejected by graph validation as a duplicate (from, to) pair.
        # Event carries route and state on `actions`, not at the top level.
        assert event.actions.route == "valid"


def test_trigger_router_rejects_an_unknown_source():
    bad = TriggerEvent.model_construct(trigger_source="CRON", repo="o/r", run_id="1")
    assert trigger_router(bad).actions.route == "invalid_trigger"


def test_branch_guard_reads_the_authoritative_pr_number():
    assert branch_guard({}, FakeCtx()).actions.state_delta["has_pr"] is True
    ctx = FakeCtx(state={"run_context": dict(RUN_CONTEXT, pr_number=None)})
    assert branch_guard({}, ctx).actions.state_delta["has_pr"] is False


async def test_verifier_passes_a_clean_finding():
    s = await verify(CodeFinding(verdict="INFRA_SUSPECTED", confidence=0.8,
                                 one_line_summary="runner evicted"), FakeCtx())
    assert s.audit_passed and s.audit_notes == []


async def test_verifier_rejects_an_introduction_claim_with_no_baseline():
    """The safe failure mode: no baseline means no verified introduction."""
    s = await verify(CodeFinding(verdict="INTRODUCED_BY_COMMIT", confidence=0.9,
                                 one_line_summary="the PR broke it"), FakeCtx())
    assert not s.audit_passed
    assert any("no baseline check" in n for n in s.audit_notes)


async def test_verifier_rejects_a_sha_this_run_does_not_contain():
    """A sha read out of attacker-controlled log text is not blameable."""
    s = await verify(CodeFinding(offending_sha="d" * 40, confidence=0.5,
                                 one_line_summary="x"), FakeCtx())
    assert any("not in this run's commit set" in n for n in s.audit_notes)
    ok = await verify(CodeFinding(offending_sha=HEAD, confidence=0.5,
                                  one_line_summary="x"), FakeCtx())
    assert ok.audit_passed


async def test_verifier_checks_the_quoted_assertion_against_log_bytes():
    log_key = evidence_filename(run_id="99", kind="ci_log", pinned_to="5",
                                key="job.log")
    ctx = FakeCtx(artifacts={log_key: b"--- FAIL: TestX\n    want 3, got 4\n"})
    good = await verify(CodeFinding(assertion_actual="want 3, got 4",
                                    confidence=0.5, one_line_summary="x"), ctx)
    assert good.audit_passed
    bad = await verify(CodeFinding(assertion_actual="want 9, got 1",
                                   confidence=0.5, one_line_summary="x"), ctx)
    assert any("not a substring" in n for n in bad.audit_notes)


async def test_verifier_resolves_evidence_the_model_misnamed():
    """Citations: filename is the path or job-<id>.log."""
    path = "cmd/ateom-microvm/snapshotcache.go"
    source_key = evidence_filename(run_id="99", kind="source_file",
                                   pinned_to=HEAD, key=path)
    log_key = evidence_filename(run_id="99", kind="ci_log", pinned_to="5",
                                key="job.log")
    literal = "cannot find module providing package example.com/missing"
    ctx = FakeCtx(artifacts={
        source_key: b"package main\n",
        log_key: literal.encode(),
    })
    finding = CodeFinding(
        confidence=0.5, one_line_summary="x", assertion_actual=literal,
        evidence=[
            EvidenceRef(kind="source_file", key=path, pinned_to=HEAD,
                        filename=path),
            EvidenceRef(kind="ci_log", key="5", pinned_to=HEAD,
                        filename="job-5.log"),
        ],
    )
    s = await verify(finding, ctx)
    assert not any("cited evidence missing" in n for n in s.audit_notes)

    missing = await verify(CodeFinding(
        confidence=0.5, one_line_summary="x",
        evidence=[EvidenceRef(kind="source_file", key="no/such.go",
                              pinned_to=HEAD, filename="no/such.go")],
    ), ctx)
    assert any("cited evidence missing" in n for n in missing.audit_notes)


async def test_verifier_rejects_a_path_outside_the_pinned_diff():
    s = await verify(CodeFinding(offending_paths=["pkg/invented.go"],
                                 confidence=0.5, one_line_summary="x"), FakeCtx())
    assert any("not in the pinned diff" in n for n in s.audit_notes)
    ok = await verify(CodeFinding(offending_paths=["pkg/real.go"],
                                  confidence=0.5, one_line_summary="x"), FakeCtx())
    assert ok.audit_passed


async def test_verifier_keeps_notes_raised_upstream():
    summary = InvestigationSummary(
        failure_id="primary", confidence=1.0, one_line_summary="x",
        audit_notes=["model returned a confidence outside 0.0-1.0; clamped"],
        finding=CodeFinding(confidence=1.0, one_line_summary="x"))
    events = [e async for e in nodes.verifier_linter(summary.model_dump(), FakeCtx())]
    out = InvestigationSummary(**events[-1].output)
    assert not out.audit_passed and "clamped" in out.audit_notes[0]


def test_failed_audit_is_banner_flagged_not_dropped():
    s = InvestigationSummary(failure_id="p", confidence=0.9, one_line_summary="x",
                             audit_passed=False, audit_notes=["no baseline check"],
                             finding=CodeFinding(verdict="INTRODUCED_BY_COMMIT"))
    assert _banner(s).startswith("LOW CONFIDENCE")
    assert "no baseline check" in render_report(s)


def test_verified_introduction_gets_no_banner():
    s = InvestigationSummary(failure_id="p", confidence=0.9, one_line_summary="x",
                             audit_passed=True,
                             finding=CodeFinding(verdict="INTRODUCED_BY_COMMIT"))
    assert _banner(s) == ""


def test_quota_exhausted_gets_its_own_banner_ahead_of_audit_status():
    """The 429 exit must read as its own thing, not fall through to LOW
    CONFIDENCE (audit_passed defaults False) or an unbannered pass-through."""
    s = InvestigationSummary(failure_id="p", confidence=0.0, one_line_summary="x",
                             exit_reason="quota_exhausted", audit_passed=False,
                             audit_notes=["model backend returned HTTP 429"])
    assert _banner(s).startswith("MODEL QUOTA EXHAUSTED")


def test_llm_call_limit_exceeded_gets_its_own_banner():
    """The runaway-loop breaker must read as distinct from quota exhaustion -
    same shape of failure (routed exit, not a crash), different cause."""
    s = InvestigationSummary(failure_id="p", confidence=0.0, one_line_summary="x",
                             exit_reason="llm_call_limit_exceeded", audit_passed=False,
                             audit_notes=["hit the ADK_MAX_LLM_CALLS ceiling"])
    assert _banner(s).startswith("RUNAWAY LOOP STOPPED")


def test_merge_summary_preserves_the_other_failure_groups():
    """A single-key write would replace the whole map - the shipped §0.1 bug."""
    ctx = FakeCtx(state={"investigation_summaries": {"a": {"failure_id": "a"}}})
    delta = merge_summary(ctx, InvestigationSummary(
        failure_id="b", confidence=0.1, one_line_summary="x"))
    assert set(delta["investigation_summaries"]) == {"a", "b"}


async def test_record_outcome_awaits_human_before_feedback(monkeypatch):
    seen = {}

    def fake_write(**kwargs):
        seen.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr("e2e_agent.analytics.writer.record_outcome", fake_write)
    ctx = FakeCtx(state={"run_context": RUN_CONTEXT, "governance": {
        "session_id": "sess-1", "run_id": "99", "trigger_source": "TEST_OPERATOR",
        "budget": {"max_turns": 3, "current_turn": 1, "token_limit": 0, "tokens_used": 0},
    }})
    summary = InvestigationSummary(
        failure_id="primary", confidence=0.4, one_line_summary="x").model_dump()
    events = [e async for e in nodes.record_outcome(summary, ctx)]
    assert events[-1].actions.route == "await_human"
    assert seen["summary"]["failure_id"] == "primary"
    assert seen["trace"] == []


async def test_record_outcome_forwards_file_hops_from_the_session(monkeypatch):
    seen = {}

    def fake_write(**kwargs):
        seen.update(kwargs)
        return {"ok": True}

    class Call:
        def __init__(self, name, args):
            self.name = name
            self.args = args

    class Event:
        author = "investigator"

        def get_function_calls(self):
            return [Call("outline_file", {"path": "pkg/x.go", "sha": "abc"})]

    class Session:
        events = [Event()]

    monkeypatch.setattr("e2e_agent.analytics.writer.record_outcome", fake_write)
    ctx = FakeCtx(state={"run_context": RUN_CONTEXT, "governance": {
        "session_id": "sess-1", "run_id": "99", "trigger_source": "TEST_OPERATOR",
    }})
    ctx.session = Session()
    summary = InvestigationSummary(
        failure_id="primary", confidence=0.4, one_line_summary="x").model_dump()
    events = [e async for e in nodes.record_outcome(summary, ctx)]
    assert events[-1].actions.route == "await_human"
    assert seen["trace"][0]["calls"][0]["args"]["path"] == "pkg/x.go"


async def test_record_outcome_copies_adk_state_without_indexing_it(monkeypatch):
    """ADK State is not iterable. dict(state) asks for key 0 and crashes the node."""
    seen = {}

    def fake_write(**kwargs):
        seen.update(kwargs)
        return {"ok": True}

    class AdkState:
        def __init__(self, data):
            self._data = data

        def get(self, key, default=None):
            return self._data.get(key, default)

        def to_dict(self):
            return dict(self._data)

        def __getitem__(self, key):
            raise KeyError(key)

    monkeypatch.setattr("e2e_agent.analytics.writer.record_outcome", fake_write)
    ctx = FakeCtx(state=AdkState({"run_context": RUN_CONTEXT}))
    summary = InvestigationSummary(
        failure_id="primary", confidence=0.4, one_line_summary="x").model_dump()
    events = [e async for e in nodes.record_outcome(summary, ctx)]
    assert events[-1].actions.route == "await_human"
    assert seen["state"]["run_context"]["repo"] == "o/r"


async def test_record_outcome_finishes_after_deep_dive_feedback(monkeypatch):
    monkeypatch.setattr("e2e_agent.analytics.writer.record_outcome",
                        lambda **kwargs: {"ok": True})
    ctx = FakeCtx(state={"run_context": RUN_CONTEXT,
                         "engineer_feedback": "look at the listen address"})
    summary = InvestigationSummary(
        failure_id="primary", confidence=0.4, one_line_summary="x",
        exit_reason="deep_dive").model_dump()
    events = [e async for e in nodes.record_outcome(summary, ctx)]
    assert events[-1].actions.route == "done"


def _summary() -> dict:
    return InvestigationSummary(
        failure_id="primary", confidence=0.4,
        one_line_summary="listen address drifted",
        finding=CodeFinding(
            verdict="INTRODUCED_BY_COMMIT", reasoning="a long trail",
            evidence=[EvidenceRef(
                kind="source_file", key="pkg/x.go", pinned_to=HEAD,
                filename="ev/x")],
        ),
    ).model_dump()


def test_hitl_form_is_two_fields():
    schema = DeepDiveFeedback.model_json_schema()
    assert set(schema["properties"]) == {"decision", "focus"}
    assert schema["properties"]["decision"]["enum"] == ["finish", "dig_deeper"]


def _gate(ctx, summary):
    return nodes.dormancy_gate._func(ctx, summary)


async def test_dormancy_gate_asks_with_report_not_summary_payload():
    ctx = FakeCtx(state={"dormancy_retry_count": 0})
    events = [e async for e in _gate(ctx, _summary())]
    ask = events[-1]
    assert ask.payload is None
    assert ask.response_schema is DeepDiveFeedback
    assert "listen address drifted" in ask.message
    assert "pkg/x.go" not in ask.message


async def test_dormancy_gate_finish_skips_deep_dive():
    ctx = FakeCtx(
        state={"dormancy_retry_count": 0},
        resume_inputs={"deep_dive_request_0": {
            "decision": "finish", "focus": ""}},
    )
    events = [e async for e in _gate(ctx, _summary())]
    assert events[-1].actions.route == "finish"
    assert events[-1].actions.state_delta["engineer_feedback"] == "finish"


async def test_dormancy_gate_dig_deeper_requires_a_focus():
    ctx = FakeCtx(
        state={"dormancy_retry_count": 0},
        resume_inputs={"deep_dive_request_0": {
            "decision": "dig_deeper", "focus": "  "}},
    )
    events = [e async for e in _gate(ctx, _summary())]
    assert events[-1].actions.route == "invalid_feedback"
    assert events[-1].actions.state_delta["dormancy_retry_count"] == 1


async def test_dormancy_gate_dig_deeper_passes_focus():
    ctx = FakeCtx(
        state={"dormancy_retry_count": 0},
        resume_inputs={"deep_dive_request_0": {
            "decision": "dig_deeper", "focus": " pkg/listen.go "}},
    )
    events = [e async for e in _gate(ctx, _summary())]
    assert events[-1].actions.route == "resume_valid"
    assert events[-1].actions.state_delta["engineer_feedback"] == "pkg/listen.go"


async def test_dormancy_gate_accepts_legacy_reply():
    ctx = FakeCtx(
        state={"dormancy_retry_count": 0},
        resume_inputs={"deep_dive_request_0": {
            "engineer_feedback": "no"}},
    )
    events = [e async for e in _gate(ctx, _summary())]
    assert events[-1].actions.route == "finish"
