"""The graph's nodes.

Shape follows docs/graph-workflow-with-hitl.md:
pin before branching, routed budget exit rather than a raised exception, a
deterministic (non-LLM) verifier, and HITL mechanics left exactly as shipped.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from google.adk import Context, Event
from google.adk.agents import LlmAgent
from google.adk.agents.invocation_context import LlmCallsLimitExceededError
from google.adk.events import RequestInput
from google.adk.models import Gemini
from google.adk.planners import BuiltInPlanner
from google.adk.workflow import FunctionNode, RetryConfig, node
from google.genai import types as genai_types
from google.genai.errors import ClientError
from pydantic import ValidationError

from ..code.failure_address import parse_go_failures, strip_timestamps
from ..github.client import GitHub
from ..run_context import RunContext, pin_run_context
from ..store import evidence_candidates, evidence_filename
from ..tools.adk_tools import INVESTIGATOR_TOOLS, PR_SCOPED_TOOLS
from ..tools.artifact_store import ArtifactStore
from .prompts import DEEP_DIVE_INSTRUCTION, INVESTIGATOR_INSTRUCTION
from .state import (
    CodeFinding,
    DeepDiveFeedback,
    FailureGroup,
    InvestigationSummary,
    TriggerEvent,
    merge_summary,
)

MODEL = os.environ.get("E2E_AGENT_MODEL", "gemini-3.7-flash")
MODEL_LOCATION = os.environ.get("E2E_AGENT_LOCATION") or os.environ.get("GOOGLE_CLOUD_LOCATION") or "global"
MAX_TURNS = int(os.environ.get("E2E_AGENT_MAX_TURNS", "3"))
# Gemini's Dynamic Shared Quota pool returns 429s under normal contention, not
# just abuse. Without retry_options the genai client retries zero times
# (google/genai/_api_client.py: retry_args(None) -> stop_after_attempt(1)), so
# every transient 429/500/503 would otherwise crash the graph outright.
MODEL_RETRY_ATTEMPTS = int(os.environ.get("E2E_AGENT_MODEL_RETRY_ATTEMPTS", "6"))

model_adapter = Gemini(
    model=MODEL,
    client_kwargs={"location": MODEL_LOCATION},
    retry_options=genai_types.HttpRetryOptions(
        attempts=MODEL_RETRY_ATTEMPTS, initial_delay=2.0, max_delay=30.0,
    ),
)

thinking_planner = BuiltInPlanner(
    thinking_config=genai_types.ThinkingConfig(include_thoughts=True),
)

VALID_TRIGGERS = {"TEST_OPERATOR", "SLACK_MANUAL"}


# ---- 1. trigger_router ----------------------------------------------------

def trigger_router(node_input: TriggerEvent):
    """Validate the entry event.

    Both valid triggers route to the SAME successor, so this emits one 'valid'
    route rather than one route per trigger source: ADK's graph validation
    rejects two edges with the same (from, to) pair, which is what routing
    TEST_OPERATOR and SLACK_MANUAL separately to pin_run_context would build.
    The trigger source is carried in state and re-emitted as a route by
    pin_run_context, where the paths genuinely diverge.
    """
    if node_input.trigger_source not in VALID_TRIGGERS:
        return Event(route="invalid_trigger", output=node_input.model_dump())
    return Event(route="valid", output=node_input.model_dump())


def trigger_error(node_input: dict):
    """Terminal. A named, loud failure rather than a KeyError in edge dispatch."""
    return {"error": f"unrecognized trigger_source: {node_input!r}"}


# ---- 2. pin_run_context ---------------------------------------------------
# Before branch_guard, so has_pr comes from the authoritative pr_number rather
# than from the trigger payload, which can disagree with it.

async def pin_run(node_input: dict, ctx: Context):
    """Resolve the run to frozen coordinates and store the run metadata."""
    trigger = TriggerEvent(**node_input)
    gh = GitHub(trigger.repo)
    try:
        rc = await pin_run_context(gh, trigger.run_id)
    except RuntimeError as exc:
        yield Event(route="no_failed_jobs",
                    output={"error": str(exc), "run_id": trigger.run_id})
        return
    finally:
        await gh.aclose()

    await ArtifactStore(ctx).put(
        evidence_filename(run_id=rc.run_id, kind="run_metadata",
                          pinned_to=rc.head_sha, key="run_context.json"),
        rc.model_dump_json(indent=2).encode(),
    )
    yield Event(
        route=trigger.trigger_source,
        output=rc.model_dump(),
        state={
            "run_context": rc.model_dump(),
            "governance": {
                "session_id": ctx.session.id,
                "run_id": rc.run_id,
                "trigger_source": trigger.trigger_source,
                "budget": {"max_turns": MAX_TURNS, "current_turn": 0,
                           "token_limit": 0, "tokens_used": 0},
            },
        },
    )


pin_run_context_node = FunctionNode(
    func=pin_run,
    name="pin_run_context",
    # Without this, one GitHub 5xx kills the graph before any evidence exists,
    # on the critical path of an active incident.
    retry_config=RetryConfig(max_attempts=3, initial_delay=2.0,
                             backoff_factor=2.0,
                             exceptions=["TimeoutError", "ConnectionError"]),
)


def pin_error(node_input: dict):
    """Terminal. GitHub unreachable, or the run has no failed jobs."""
    return {"error": node_input.get("error", "could not pin run")}


# ---- 3. failure_router ----------------------------------------------------

async def failure_router(node_input: dict, ctx: Context):
    """Group a multi-failure run into FailureGroups.

    DEVIATION from the shipped design, which specifies an LLM classifier here:
    the log parser already yields exact test names, packages and assertion lines,
    and an LLM would paraphrase the literals the method forbids paraphrasing.
    Grouping is one group per failing test, plus one per failing job that names
    no test at all (the infrastructure case).
    """
    rc = RunContext(**ctx.state["run_context"])
    gh = GitHub(rc.repo)
    store = ArtifactStore(ctx)
    groups: list[FailureGroup] = []
    try:
        for job in rc.failed_jobs:
            key = evidence_filename(run_id=rc.run_id, kind="ci_log",
                                    pinned_to=str(job.job_id), key="job.log")
            raw = await store.get(key)
            if raw is None:
                raw = (await gh.job_log(job.job_id)).encode("utf-8", "replace")
                await store.put(key, raw)
            log = strip_timestamps(raw.decode("utf-8", "replace"))
            addrs = parse_go_failures(log, rc.module)
            tests = [a for a in addrs if a.kind == "test"]
            for a in tests:
                groups.append(FailureGroup(
                    failure_id=f"{job.job_id}:{a.test_address}",
                    test_name=a.test_address or "unknown",
                    error_message=(a.context[-1] if a.context else a.raw)[:400],
                    failure_type="GINKGO_TEST",
                ))
            if not tests:
                groups.append(FailureGroup(
                    failure_id=f"{job.job_id}:no_test",
                    test_name=job.name,
                    error_message=f"job {job.name} {job.conclusion} with no test "
                                  "assertion in its log",
                    failure_type="INFRASTRUCTURE",
                ))
    finally:
        await gh.aclose()

    yield Event(output=node_input, state={"failure_groups":
                                          [g.model_dump() for g in groups]})


# ---- 4. branch_guard ------------------------------------------------------

def branch_guard(node_input: dict, ctx: Context):
    """Set has_pr from the authoritative run_context. Sets state only; it never
    reaches into another node's toolset."""
    rc = ctx.state["run_context"]
    return Event(output=node_input, state={"has_pr": rc.get("pr_number") is not None})


def deny_pr_tool_if_absent(tool: Any, args: dict, tool_context: Any):
    """before_tool_callback: refuse diff tools when the run has no PR."""
    if tool.name in PR_SCOPED_TOOLS and not tool_context.state.get("has_pr"):
        return {"error": "No PR for this run - tool disabled. The run still has "
                         "a base_sha and a head_sha; investigate from the log."}
    return None


# ---- 5. investigator_loop -------------------------------------------------

investigator_agent = LlmAgent(
    name="investigator",
    model=model_adapter,
    instruction=INVESTIGATOR_INSTRUCTION,
    tools=INVESTIGATOR_TOOLS,
    output_schema=CodeFinding,
    planner=thinking_planner,
    before_tool_callback=deny_pr_tool_if_absent,
)


@node(rerun_on_resume=True, name="investigator_loop")
async def investigator_loop(ctx: Context, node_input: dict):
    """Bounded investigation with a ROUTED exit.

    Budget exhaustion is an expected terminal condition, not a crash: it routes
    to the verifier and reaches a human with a banner, rather than raising.
    `rerun_on_resume=True` is required because this node schedules another node
    dynamically (ctx.run_node).
    """
    rc = RunContext(**ctx.state["run_context"])
    governance = dict(ctx.state.get("governance") or {})
    budget = dict(governance.get("budget") or
                  {"max_turns": MAX_TURNS, "current_turn": 0,
                   "token_limit": 0, "tokens_used": 0})

    groups = ctx.state.get("failure_groups") or []
    focus = groups[0]["test_name"] if groups else "the run's failed jobs"
    jobs = [{"job_id": j.job_id, "name": j.name, "conclusion": j.conclusion}
            for j in rc.failed_jobs]
    brief = (f"Repo {rc.repo}, run {rc.run_id}. head_sha={rc.head_sha} "
             f"base_sha={rc.base_sha} ({rc.base_ref_name}). "
             f"pr_number={rc.pr_number}. Failed jobs: {jobs}. "
             f"Focus on: {focus}. job_id values are already here - do not call "
             f"list_failed_jobs; emit list_failure_addresses, list_changed_files "
             f"and find_in_log in the same turn.")

    finding: CodeFinding | None = None
    quota_exhausted = False
    llm_calls_exhausted = False
    while budget["current_turn"] < budget["max_turns"]:
        budget["current_turn"] += 1
        try:
            raw = await ctx.run_node(investigator_agent, node_input=brief)
        except ClientError as exc:
            if exc.code != 429:
                raise
            # google_llm.py already retried with exponential backoff before
            # raising; a 429 here means the DSQ pool stayed exhausted through
            # all of them. Routed exit, same as budget exhaustion, not a crash.
            quota_exhausted = True
            break
        except LlmCallsLimitExceededError:
            # ADK_MAX_LLM_CALLS is a whole-invocation breaker, not per-turn:
            # one node turn can silently burn many raw Gemini calls (one per
            # tool round trip), invisible to budget["current_turn"]. This is
            # the backstop for that - a runaway tool-calling loop, not a
            # quota problem, so it gets its own exit_reason.
            llm_calls_exhausted = True
            break
        try:
            finding = CodeFinding.model_validate(
                raw if isinstance(raw, dict) else {"one_line_summary": str(raw)}
            )
        except ValidationError as exc:
            brief += f"\n\nYour last answer did not validate ({exc}). Try again."
            continue
        if finding.verdict != "INDETERMINATE" or finding.confidence >= 0.5:
            break
        brief += ("\n\nThat verdict was INDETERMINATE with low confidence. "
                  "Name the single missing fact and go get it.")

    governance["budget"] = budget
    if finding is None:
        finding = CodeFinding(one_line_summary="investigator produced no finding")

    # Observed on a live run: the model returned confidence=5.0 on a 0-1 field.
    # Clamping silently would hide it, and a banner that says 0.9 when the model
    # said 5.0 is a lie about the only number a human skims.
    out_of_range = not 0.0 <= finding.confidence <= 1.0
    if out_of_range:
        finding.confidence = min(max(finding.confidence, 0.0), 1.0)

    if quota_exhausted:
        exit_reason = "quota_exhausted"
    elif llm_calls_exhausted:
        exit_reason = "llm_call_limit_exceeded"
    elif finding.verdict == "INFRA_SUSPECTED":
        exit_reason = "infra_suspected"
    elif (budget["current_turn"] >= budget["max_turns"]
          and finding.verdict == "INDETERMINATE"):
        exit_reason = "budget_exceeded"
    else:
        exit_reason = "investigation_complete"

    audit_notes = ["model returned a confidence outside 0.0-1.0; clamped"] if out_of_range else []
    if quota_exhausted:
        audit_notes.append(
            "model backend returned HTTP 429 (Dynamic Shared Quota exhausted) "
            f"and stayed exhausted through {MODEL_RETRY_ATTEMPTS} backoff retries"
        )
    elif llm_calls_exhausted:
        audit_notes.append("hit the ADK_MAX_LLM_CALLS ceiling for this "
                            "invocation before reaching a verdict")

    summary = InvestigationSummary(
        failure_id=finding.failure_id,
        exit_reason=exit_reason,
        audit_notes=audit_notes,
        confidence=finding.confidence,
        one_line_summary=finding.one_line_summary,
        budget=budget,
        finding=finding,
    )
    # No route: all five exits go to the verifier, and ADK forbids two edges
    # sharing a (from, to) pair. exit_reason carries the distinction.
    yield Event(output=summary.model_dump(), state={"governance": governance})


# ---- 6. verifier_linter ---------------------------------------------------
# Deterministic and non-LLM. Referential integrity is not evidential support: a
# model can cite a real artifact that does not contain what it claims, and this
# banner is the only thing a human uses to tell a trustworthy report from a
# fabricated one.

async def verifier_linter(node_input: dict, ctx: Context):
    summary = InvestigationSummary(**node_input)
    rc = RunContext(**ctx.state["run_context"])
    store = ArtifactStore(ctx)
    notes: list[str] = list(summary.audit_notes)   # keep upstream complaints
    f = summary.finding

    if f is None:
        notes.append("no finding produced")
    else:
        # (a) referential - every cited ref resolves to real bytes.
        # The model invents filenames (`job-<id>.log`, the repo path). The
        # store key is evidence_filename; try that before calling it missing.
        for ref in f.evidence:
            names = evidence_candidates(
                run_id=rc.run_id, kind=ref.kind, key=ref.key,
                pinned_to=ref.pinned_to, filename=ref.filename)
            resolved = False
            for name in names:
                if await store.get(name) is not None:
                    resolved = True
                    break
            if not resolved:
                notes.append(f"cited evidence missing: {ref.kind}:{ref.key}")

        # (b) EVIDENTIAL - the quoted literal is really in the log bytes
        if f.assertion_actual:
            found = False
            for job in rc.failed_jobs:
                blob = await store.get(evidence_filename(
                    run_id=rc.run_id, kind="ci_log",
                    pinned_to=str(job.job_id), key="job.log"))
                if blob and f.assertion_actual in blob.decode("utf-8", "replace"):
                    found = True
                    break
            if not found:
                notes.append("assertion_actual is not a substring of any read ci_log")

        # (c) INJECTION GUARD - a sha read out of attacker-controlled test output
        #     is not a sha this run may blame.
        for sha in filter(None, (f.offending_sha,)):
            if sha not in rc.known_shas:
                notes.append(f"sha {sha[:12]} is not in this run's commit set")

        # (d) BASELINE CONJUNCTION - decides whether a human is told "this change
        #     broke it" with or without a low-confidence banner.
        if f.verdict == "INTRODUCED_BY_COMMIT":
            b = f.baseline
            if b is None:
                notes.append("introduction claim with no baseline check")
            elif not b.supports_introduction:
                notes.append(
                    f"baseline inconclusive: search_ok={b.search_ok} "
                    f"truncated={b.truncated} present_on_head={b.present_on_head} "
                    f"present_on_base={b.present_on_base}")

        # (e) manifest - every blamed path is really in the pinned diff
        if f.offending_paths:
            gh = GitHub(rc.repo)
            try:
                changed = {c.path for c in
                           await gh.compare_files(rc.base_sha, rc.head_sha)}
            finally:
                await gh.aclose()
            for p in f.offending_paths:
                if p not in changed:
                    notes.append(f"blamed path is not in the pinned diff: {p}")

    summary.audit_passed = not notes
    summary.audit_notes = notes
    # A failed audit is neither dropped nor posted indistinguishably from a
    # verified one: both go to the gate, and the banner carries audit_notes.
    yield Event(output=summary.model_dump(),
                state=merge_summary(ctx, summary))   # whole-map write


# ---- 6b. record_outcome ---------------------------------------------------
# Deterministic warehouse write. Not an LLM tool: SQL is fixed, values come
# from the summary and from code-tool calls already on the session. A BigQuery
# failure must not take down the graph.

def _state_mapping(state) -> dict:
    """Copy session state to a plain dict.

    ADK's State has no keys() or __iter__. dict(state) falls back to
    state[0], state[1], ... and raises KeyError: 0.
    """
    to_dict = getattr(state, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return dict(state or {})


async def record_outcome(node_input: dict, ctx: Context):
    """Persist the verified (or deep-dived) finding, then route onward.

    After the verifier the human is still waiting, so the route is
    `await_human`. After a deep dive `engineer_feedback` is already in
    state and the route is `done`.

    File hops are taken from the session's tool calls so far. The deep-dive
    write replaces them with the full path, including files opened only then.
    """
    from ..analytics.investigation_rows import session_trace
    from ..analytics.writer import record_outcome as write_outcome
    session = getattr(ctx, "session", None)
    trace = session_trace(getattr(session, "events", None))
    await asyncio.to_thread(
        write_outcome, state=_state_mapping(ctx.state), summary=node_input,
        trace=trace)
    route = "done" if ctx.state.get("engineer_feedback") else "await_human"
    yield Event(route=route, output=node_input)


record_outcome_node = FunctionNode(
    func=record_outcome,
    name="record_outcome",
    retry_config=RetryConfig(max_attempts=2, initial_delay=1.0,
                             backoff_factor=2.0),
)


# ---- 7. dormancy_gate -----------------------------------------------------

def _banner(s: InvestigationSummary) -> str:
    v = s.finding.verdict if s.finding else "INDETERMINATE"
    if s.exit_reason == "quota_exhausted":
        return ("MODEL QUOTA EXHAUSTED - the Gemini backend kept returning HTTP "
                "429 (Dynamic Shared Quota) after repeated backoff retries. This "
                "is a shared-pool limit, not specific to this run. Re-run the "
                "agent, or see "
                "https://docs.cloud.google.com/gemini-enterprise-agent-platform/"
                "models/quotas.\n")
    if s.exit_reason == "llm_call_limit_exceeded":
        return ("RUNAWAY LOOP STOPPED - this investigation hit the "
                "ADK_MAX_LLM_CALLS ceiling before reaching a verdict. This is "
                "a safety breaker, not a quota issue: something made the "
                "agent chain far more tool calls than a normal investigation "
                "needs. Check the trace before raising the ceiling.\n")
    if not s.audit_passed:
        return ("LOW CONFIDENCE - verifier could not confirm cited evidence: "
                + "; ".join(s.audit_notes) + "\n")
    if v == "INFRA_SUSPECTED":
        return "INFRASTRUCTURE SUSPECTED - no test assertion found.\n"
    if v == "FLAKE_SUSPECTED":
        return "FLAKE SUSPECTED - this test also fails on the base branch.\n"
    if v == "PRE_EXISTING":
        return "PRE-EXISTING - the failing literal is already present on base.\n"
    if v == "INDETERMINATE":
        return "INCONCLUSIVE - no verdict reached within budget.\n"
    return ""


def render_report(s: InvestigationSummary) -> str:
    f = s.finding
    lines = [_banner(s).rstrip("\n"), f"**{s.one_line_summary}**"]
    if f:
        lines += [
            f"verdict: {f.verdict}   confidence: {f.confidence:.2f}",
            f"failed test: {f.failed_test or '-'}",
            f"symbols: {', '.join(f.offending_symbols) or '-'}",
            f"paths: {', '.join(f.offending_paths) or '-'}",
        ]
        if f.assertion_expected or f.assertion_actual:
            lines.append(f"expected: {f.assertion_expected!r}  "
                         f"actual: {f.assertion_actual!r}")
        if f.reasoning:
            lines += ["", f.reasoning]
    return "\n".join(x for x in lines if x)


@node(rerun_on_resume=True, name="dormancy_gate")
async def dormancy_gate(ctx: Context, node_input: dict):
    """Post the report and pause for the engineer, releasing compute meanwhile.

    interrupt_id varies per re-prompt: an id reused across iterations wedges the
    loop forever on resume. The idempotency key is keyed on the interrupt_id -
    the thing that identifies 'this prompt to this human' - not on invocation_id.
    """
    summary = InvestigationSummary(**node_input)
    retry_count = ctx.state.get("dormancy_retry_count", 0)
    interrupt_id = f"deep_dive_request_{retry_count}"
    resume = (ctx.resume_inputs or {}).get(interrupt_id)

    if resume is None:
        # payload stays unset. The report is the message; the form is
        # DeepDiveFeedback. A model_dump() of the summary here becomes one
        # input per nested field in Gemini Enterprise.
        yield RequestInput(
            interrupt_id=interrupt_id,
            message=render_report(summary) +
            "\n\nChoose finish to close this, or dig_deeper and name what "
            "to look at.",
            response_schema=DeepDiveFeedback,
        )
        return

    try:
        feedback = _feedback_from_resume(resume)
    except ValidationError:
        yield Event(route="invalid_feedback",
                    output={"error": "malformed engineer reply"},
                    state={"dormancy_retry_count": retry_count + 1})
        return

    if feedback.decision == "finish":
        # Truthy so the follow-up warehouse write records paused=False.
        note = feedback.focus.strip() or "finish"
        yield Event(route="finish", output=node_input,
                    state={"engineer_feedback": note})
        return

    if not feedback.focus.strip():
        yield Event(route="invalid_feedback",
                    output={"error": "dig_deeper requires a focus"},
                    state={"dormancy_retry_count": retry_count + 1})
        return

    yield Event(route="resume_valid", output=node_input,
                state={"engineer_feedback": feedback.focus.strip()})


_FINISH_WORDS = frozenset({"no", "n", "finish", "done", "stop"})


def _feedback_from_resume(resume: Any) -> DeepDiveFeedback:
    """Accept the two-field form, and the older single-string reply.

    `run_local --feedback` and a resumed session from before the form
    change send `{"engineer_feedback": "..."}` or a bare string. "no"
    meant finish; anything else was the deep-dive focus.
    """
    if isinstance(resume, dict) and "decision" in resume:
        return DeepDiveFeedback.model_validate(resume)
    if isinstance(resume, dict):
        text = str(resume.get("engineer_feedback") or resume.get("focus") or "")
    else:
        text = str(resume)
    text = text.strip()
    if text.lower() in _FINISH_WORDS or not text:
        return DeepDiveFeedback(decision="finish", focus="")
    return DeepDiveFeedback(decision="dig_deeper", focus=text)


# ---- 8. targeted_deep_dive ------------------------------------------------

deep_dive_agent = LlmAgent(
    name="deep_dive",
    model=model_adapter,
    instruction=DEEP_DIVE_INSTRUCTION,
    tools=INVESTIGATOR_TOOLS,
    output_schema=CodeFinding,
    planner=thinking_planner,
    before_tool_callback=deny_pr_tool_if_absent,
)


@node(rerun_on_resume=True, name="targeted_deep_dive")
async def targeted_deep_dive(ctx: Context, node_input: dict):
    summary = InvestigationSummary(**node_input)
    feedback = ctx.state.get("engineer_feedback") or ""
    rc = RunContext(**ctx.state["run_context"])
    jobs = [{"job_id": j.job_id, "name": j.name, "conclusion": j.conclusion}
            for j in rc.failed_jobs]
    brief = (f"Repo {rc.repo}, run {rc.run_id}, head_sha={rc.head_sha}, "
             f"base_sha={rc.base_sha}. Failed jobs: {jobs}.\n"
             f"Your earlier finding: {summary.model_dump_json()}\n"
             f"The engineer replied: {feedback}")
    try:
        raw = await ctx.run_node(deep_dive_agent, node_input=brief)
    except ClientError as exc:
        if exc.code != 429:
            raise
        # Same routed exit as investigator_loop: the genai client already
        # retried with backoff, so this is a sustained DSQ outage, not a blip.
        finding = summary.finding
        updated = InvestigationSummary(
            failure_id=summary.failure_id,
            confidence=summary.confidence,
            one_line_summary=summary.one_line_summary,
            exit_reason="quota_exhausted",
            audit_passed=False,
            audit_notes=["deep-dive could not run: model backend returned HTTP "
                         f"429 (DSQ exhausted) through {MODEL_RETRY_ATTEMPTS} "
                         "backoff retries"],
            budget=summary.budget,
            finding=finding,
        )
        yield Event(output=updated.model_dump(), state=merge_summary(ctx, updated))
        return
    except LlmCallsLimitExceededError:
        # The counter is invocation-wide, so if investigator_loop's own turns
        # already spent it, this fires on the very first deep-dive call too.
        finding = summary.finding
        updated = InvestigationSummary(
            failure_id=summary.failure_id,
            confidence=summary.confidence,
            one_line_summary=summary.one_line_summary,
            exit_reason="llm_call_limit_exceeded",
            audit_passed=False,
            audit_notes=["deep-dive could not run: hit the ADK_MAX_LLM_CALLS "
                         "ceiling for this invocation"],
            budget=summary.budget,
            finding=finding,
        )
        yield Event(output=updated.model_dump(), state=merge_summary(ctx, updated))
        return
    try:
        finding = CodeFinding.model_validate(raw) if isinstance(raw, dict) \
            else summary.finding
    except ValidationError:
        finding = summary.finding
    updated = InvestigationSummary(
        failure_id=summary.failure_id,
        confidence=finding.confidence if finding else summary.confidence,
        one_line_summary=finding.one_line_summary if finding
        else summary.one_line_summary,
        exit_reason="deep_dive",
        audit_passed=False,
        audit_notes=["deep-dive result has not been re-verified"],
        budget=summary.budget,
        finding=finding,
    )
    yield Event(output=updated.model_dump(), state=merge_summary(ctx, updated))


# ---- 9. final_output ------------------------------------------------------

def final_output(node_input: dict):
    """Terminal, idempotent."""
    return render_report(InvestigationSummary(**node_input))
