# GEAP SRE Agent — Graph Workflow with Human-in-the-Loop

**Status:** Implemented. Source lives outside this repo; the design below is the
one that shipped and is currently working.

**Supersedes:** the nested `SequentialAgent` + `LoopAgent` architecture.

**History:** this page combines two earlier documents — the original refactor
proposal (`refactor-from-static-workflows-to-graph-workflow.md`) and the final
post-review design (`final-design.md`). The final design is authoritative;
the superseded proposal is preserved in [Appendix A](#appendix-a--superseded-proposal)
because its rejected mechanics explain several decisions below.

---

## 1. Project context

An AI SRE agent that investigates E2E test failures, distinguishes infrastructure
flakes from real regressions, and reports findings. Built with ADK, deployed to
the Gemini Enterprise Agent Platform (GEAP).

**Architectural pivot:** from nested sequential agents to an ADK graph.

* **Dual-trigger routing** — an automated test-operator trigger bypasses routing
  and investigates a single failure; a manual Slack trigger invokes the failure
  router to parse and group multi-failure logs.
* **Event-driven dormancy** — the run pauses for human feedback and releases
  compute during idle human response time, rather than holding a process open.

### Operational boundaries

* **Lead-controlled (frozen contracts):** the `AgentState` schema, non-LLM
  `branch_guard` determinism, node contracts, programmatic turn limits.
* **Engineer-owned (free iteration):** node prompt tuning, individual tool
  functions, rapid iteration inside bounded sub-graphs.

### Evals and quality assurance

* **Verifier as linter** — evaluates structural consistency between cited
  evidence and agent claims.
* **Feedback as ground truth** — engineer corrections submitted via the Slack
  deep-dive loop are the primary evaluation dataset.

---

## 2. Node list

1. `trigger_router` — validates and routes on entry-event trigger source
2. `trigger_error` — terminal node for malformed entry input
3. `failure_router` — LLM classifier, groups multi-failure Slack-triggered logs
4. `branch_guard` — non-LLM, sets `has_pr` flag (does **not** mutate other nodes' toolsets)
5. `investigator_loop` — bounded sub-graph, self-loop with routed exit (not exception-based)
6. `verifier_linter` — structural evidence-vs-claim audit, routes pass/fail
7. `dormancy_gate` — native ADK HITL node (`RequestInput`), posts report, pauses for engineer reply
8. `targeted_deep_dive` — resumes with validated engineer feedback, bounded turns
9. `final_output` — terminal node, posts deep-dive result

The graph is built with ADK's actual `Workflow(edges=[...])` / `Event(route=...)`
primitives, not a flat `TypedDict` + string-enum state machine with `if`-dispatch.

Per-node contracts (inputs, state reads/writes, side effects, idempotency,
failure behaviour) and the rendered topology live with the design-review
artifacts, trimmed from this copy of the repo.

---

## 3. State schema

```python
from typing import TypedDict, List, Dict, Optional, Literal
from pydantic import BaseModel

# --- Governance / runtime metadata (kept separate from LLM-facing context) ---

class TokenBudget(TypedDict):
    max_turns: int
    current_turn: int
    token_limit: int
    tokens_used: int

class GovernanceState(TypedDict):
    session_id: str
    run_id: str
    trigger_source: Literal["TEST_OPERATOR", "SLACK_MANUAL"]
    budget: TokenBudget

# NOTE: whenever a node updates `governance.budget`, it must construct and write
# the *entire* nested dict via Event(state={"governance": {...}}), never a dotted
# key like "governance.budget.current_turn" — state merges are a flat dict.update(),
# confirmed against in_memory_session_service.py / database_session_service.py.
# (Moot for the HITL path below, which uses resume_inputs rather than state_delta,
# but the rule still applies to every other state write.)

# --- Domain / LLM-facing context (small; large payloads go to Artifacts) ---

class FailureGroup(BaseModel):
    failure_id: str
    test_name: str
    error_message: str
    failure_type: Literal["CLUSTER_PROVISION", "GINKGO_TEST", "INFRASTRUCTURE", "UNKNOWN"]

class InvestigationSummary(BaseModel):
    failure_id: str
    confidence: float
    one_line_summary: str
    report_artifact_id: str      # full report body lives in Artifacts, not state
    audit_passed: bool           # verifier outcome is first-class, not silently dropped
    budget: TokenBudget

class DeepDiveFeedback(BaseModel):
    engineer_feedback: str

class AgentState(TypedDict):
    governance: GovernanceState
    has_pr: bool
    pr_number: Optional[str]
    commit_range: Optional[str]
    failure_groups: List[FailureGroup]
    investigation_summaries: Dict[str, InvestigationSummary]   # keyed by failure_id
    engineer_feedback: Optional[str]
```

The original proposal's `execution_status` enum (`INITIAL_RUN` /
`PAUSED_AWAITING_FEEDBACK` / `DEEP_DIVE_RUN` / `COMPLETED`) is **removed**, not
carried forward as dead weight — ADK's native `RequestInput`/`resume_inputs`
lifecycle and event trace already answer "is this run paused / where is it"
without a hand-rolled status field. Where "why did this run skip a node" needs to
be reconstructable for a postmortem, that is answered by `event.route` in the
session's event trace, not by a state field.

---

## 4. Routing logic

```python
from google.adk import Workflow, Event, Context
from google.adk.workflow import FunctionNode, RetryConfig, node
from google.adk.events import RequestInput
from google.adk.apps import App, ResumabilityConfig
from pydantic import ValidationError

# ---- Entry validation ----

VALID_TRIGGERS = {"TEST_OPERATOR", "SLACK_MANUAL"}

def trigger_router(node_input):
    if node_input.trigger_source not in VALID_TRIGGERS:
        return Event(route="invalid_trigger", output=node_input)
    return Event(route=node_input.trigger_source)

def trigger_error(node_input):
    # named, loud failure — not an opaque KeyError deep in edge dispatch
    yield Event(output={"error": f"unrecognized trigger_source: {node_input.trigger_source!r}"})

# ---- Branch Guard: sets state only, never reaches into another node ----
# ("strips tools from downstream nodes" is not a real cross-node ADK mechanism —
# replaced with a flag plus a callback owned by the consuming node.)

def branch_guard(node_input):
    has_pr = bool(node_input.pr_number)
    yield Event(output=node_input, state={"has_pr": has_pr, "pr_number": node_input.pr_number})

def deny_pr_tool_if_absent(tool, args, tool_context):
    if tool.name == "fetch_pr_diff" and not tool_context.state.get("has_pr"):
        return {"error": "No active PR for this run — tool disabled."}
# VERIFY before Phase 2 ships: exact before_tool_callback return-value contract
# against plugin_manager.py for the pinned ADK version (see Open tradeoffs #2).

# ---- Bounded Investigator: routed loop-exit, not raised exception ----
# (budget-exceeded is an expected terminal condition, not a crash.)

investigator_loop = FunctionNode(
    func=investigate_step,   # yields Event(route="continue"|"investigation_complete"|"budget_exceeded", ...)
    retry_config=RetryConfig(max_attempts=3, initial_delay=2.0, backoff_factor=2.0,
                             exceptions=["TimeoutError", "ConnectionError"]),
)

# ---- Verifier Linter: audit outcome is a real route, not a silent pass-through ----
# Unverified findings must not reach the engineer indistinguishably from verified ones.

def verifier_linter(summary: InvestigationSummary):
    audit_passed = run_structural_audit(summary)   # cited-evidence-vs-claim check
    summary.audit_passed = audit_passed
    route = "passed" if audit_passed else "failed"
    yield Event(route=route, output=summary, state={
        "investigation_summaries": {summary.failure_id: summary}
    })

# Both routes go to dormancy_gate — a failed audit is neither silently dropped nor
# posted indistinguishably from a verified one (see dormancy_gate below).

# ---- Idempotency: atomic conditional write, not check-then-act ----
# A read-then-write already_posted()/record_posted() pair is a race. Use a single
# atomic conditional insert — e.g. Cloud SQL `INSERT ... ON CONFLICT DO NOTHING
# RETURNING *`, or a Firestore transaction — and treat "0 rows returned /
# transaction aborted" as "already posted, skip".

def try_claim_idempotency_key(key: str) -> bool:
    """Returns True iff this call won the race and should perform the side effect."""
    ...  # atomic conditional write against the idempotency ledger

# ---- Dormancy Gate: native HITL, explicit payload validation on resume ----
# ctx.resume_inputs returns a plain dict, not a DeepDiveFeedback instance, and ADK's
# response_schema does NOT validate or coerce the human's reply — "the RequestInput
# class does not automatically reformat human responses to fit a specified data
# structure" (graphs/human-input.md). Validate explicitly.

@node(rerun_on_resume=True)
async def dormancy_gate(ctx: Context, summary: InvestigationSummary):
    # interrupt_id must vary per retry, or a malformed reply on attempt N+1 replays
    # against attempt N's interrupt and the loop never advances
    # (adk-agent-builder best-practices.md: "Give every loop iteration its own interrupt_id").
    retry_count = ctx.state.get("dormancy_retry_count", 0)
    interrupt_id = f"deep_dive_request_{retry_count}"
    resume = ctx.resume_inputs.get(interrupt_id)

    if resume is None:
        idem_key = f"{ctx.session_id}:{ctx.invocation_id}:dormancy_gate"
        if try_claim_idempotency_key(idem_key):
            banner = "" if summary.audit_passed else "⚠️ LOW CONFIDENCE — verifier could not confirm cited evidence.\n"
            function_call_id = slack_client.post_report(banner + summary.one_line_summary,
                                                        payload=summary)
            persist_interrupt_binding(idem_key, function_call_id)  # Slack-side lookup for later reply
        yield RequestInput(
            interrupt_id=interrupt_id,
            message="Investigation complete. Request a deep-dive?",
            payload=summary,
            response_schema=DeepDiveFeedback,   # documentation only — not enforced by ADK
        )
        return

    # resume is a plain dict — validate explicitly, ADK will not do this for us
    try:
        feedback = DeepDiveFeedback.model_validate(resume)
    except ValidationError:
        yield Event(route="invalid_feedback", output={"error": "malformed engineer reply"},
                    state={"dormancy_retry_count": retry_count + 1})
        return

    yield Event(route="resume_valid", output=feedback,
                state={"engineer_feedback": feedback.engineer_feedback})

# "invalid_feedback" loops back to dormancy_gate to re-prompt rather than letting
# unvalidated free text flow into targeted_deep_dive's prompt context unchecked.
# The re-prompt increments dormancy_retry_count so the next RequestInput gets a
# fresh interrupt_id — reusing one id across retries silently wedges the loop.

# ---- Targeted Deep-Dive: bounded, and deduped against duplicate resume delivery ----
# A duplicated FunctionResponse delivery — Slack retry, double-click — must not
# re-run this node and double-post the deep-dive result.

def targeted_deep_dive(node_input, ctx: Context):
    idem_key = f"{ctx.session_id}:deep_dive_request:targeted_deep_dive"
    if not try_claim_idempotency_key(idem_key):
        yield Event(route="continue", output=get_cached_result(idem_key))
        return
    # ... up to 3 tool turns, same routed-exit pattern as investigator_loop
    yield Event(route="continue"|"budget_exceeded", output=updated_summary,
                state={"investigation_summaries": {...}})

# ---- Final Output: terminal, idempotent Slack post ----

def final_output(summary: InvestigationSummary, ctx: Context):
    idem_key = f"{ctx.session_id}:{ctx.invocation_id}:final_output"
    if try_claim_idempotency_key(idem_key):
        slack_client.post_deep_dive_result(summary)
    yield Event(output=summary)

root_agent = Workflow(
    name="sre_investigation",
    edges=[
        ("START", trigger_router),
        (trigger_router, {
            "TEST_OPERATOR": branch_guard,
            "SLACK_MANUAL": failure_router,
            "invalid_trigger": trigger_error,
        }),
        (failure_router, branch_guard),
        (branch_guard, investigator_loop),
        (investigator_loop, {
            "continue": investigator_loop,
            "investigation_complete": verifier_linter,
            "budget_exceeded": verifier_linter,
        }),
        (verifier_linter, {
            "passed": dormancy_gate,
            "failed": dormancy_gate,
        }),
        (dormancy_gate, {
            "invalid_feedback": dormancy_gate,
            "resume_valid": targeted_deep_dive,
        }),
        (targeted_deep_dive, {
            "continue": final_output,
            "budget_exceeded": final_output,
        }),
    ],
)

app = App(
    name="sre_agent",
    root_agent=root_agent,
    # Not deferred: the resume handler runs in a separate FastAPI/Cloud Run process,
    # potentially hours or days after the pausing run. Without durability, that process
    # starts a *new* invocation from START and silently discards the original
    # investigation. Given this deployment topology, is_resumable=True is a requirement,
    # not a judgment call.
    resumability_config=ResumabilityConfig(is_resumable=True),
)
```

Client-side resume answers the pending `adk_request_input` function call directly.
This replaces the original proposal's hand-rolled `SREAgentResumeHandler` plus
dotted-key `state_delta`, which was independently confirmed broken
(see [Appendix A](#appendix-a--superseded-proposal)):

```python
response = types.Content(
    role="user",
    parts=[types.Part(function_response=types.FunctionResponse(
        id=function_call_id,   # captured at post time, persisted by the Slack bot (Open tradeoffs #1)
        name="adk_request_input",
        response={"engineer_feedback": feedback_text},
    ))],
)
async for event in runner.run_async(user_id=user_id, session_id=session_id, new_message=response):
    ...
```

---

## 5. Review findings and their resolutions

The design went through a grounding → scrutiny → adversarial → synthesis review
(the `/design-review` pipeline). Every critical finding is
either fixed above or named in [Open tradeoffs](#open-tradeoffs).

| # | Finding | Resolution |
| --- | --- | --- |
| 1 | `dormancy_gate` dict-as-attribute bug + overclaimed `response_schema` validation | Fixed: dict access + explicit `DeepDiveFeedback.model_validate()`; invalid reply routes back to re-prompt |
| 2 | No idempotency on resume path / `targeted_deep_dive` output | Fixed: idempotency key on `targeted_deep_dive` and `final_output`, keyed off `interrupt_id`/`invocation_id` |
| 3 | Check-then-act idempotency race | Fixed: replaced with atomic conditional write (`try_claim_idempotency_key`) |
| 4 | `is_resumable` framed as optional | Resolved as a requirement, given the confirmed separate-process resume topology |
| 5 | No route for failed verifier audit | Fixed: `verifier_linter` emits `passed`/`failed`; a failed audit still reaches the engineer but with an explicit low-confidence banner (alternative in Open tradeoffs #3) |
| 6 | Entry-point input never validated | Fixed: `trigger_router` validates against the literal set, routes to a named `trigger_error` node |
| 7 | Token budget silently dropped in the stage-1 revision | Fixed: `TokenBudget.token_limit`/`tokens_used` restored to schema (wiring left to Phase 2, see Open tradeoffs #4) |
| 8 | `dormancy_gate` reused a fixed `interrupt_id` across `invalid_feedback` retries, replaying a retry against the prior interrupt and wedging the loop | Fixed: `interrupt_id` suffixed with a `dormancy_retry_count` tracked in state, incremented on each `invalid_feedback` route |

---

## 6. Roadmap

| Phase | Deliverable |
| --- | --- |
| **Phase 1** | `AgentState`/domain Pydantic models; deterministic `branch_guard`; `trigger_router` validation |
| **Phase 2** | Real `Workflow(edges=[...])` graph; `investigator_loop` with routed budget-exit + `RetryConfig`; `verifier_linter` pass/fail routing; report bodies moved to Artifacts, keyed by `failure_id` |
| **Phase 3** | Native `dormancy_gate` (`RequestInput`) + `ResumabilityConfig(is_resumable=True)`; atomic idempotency ledger for every Slack-posting node (`dormancy_gate`, `targeted_deep_dive`, `final_output`); Slack-bot-side persistence of `function_call_id`/interrupt binding (new infra, not previously named anywhere — see Open tradeoffs #1) |
| **Phase 4** | Swap `InMemorySessionService` → `DatabaseSessionService`; co-locate the idempotency ledger on the same Cloud SQL instance if that is the chosen store |
| **Freeze gate** | No paging / auto-remediation / rollback node ships until the Phase 3 idempotency ledger is live and covers every node it would need — a duplicate page or duplicate rollback is a materially worse failure than a duplicate Slack message |

---

## Open tradeoffs

1. **Idempotency ledger location.** The design requires an atomic
   conditional-write store (`try_claim_idempotency_key`) and a place for the
   Slack bot to persist `function_call_id`/interrupt bindings between posting and
   a reply that may arrive days later. Candidates: Cloud SQL (co-located with
   `DatabaseSessionService`), Firestore, or Slack's own thread-reply dedup
   semantics. This is a GEAP infra decision, not resolvable from the ADK
   checkouts. **Recommendation:** Cloud SQL alongside `DatabaseSessionService` —
   one durable store instead of two, and Phase 4 already plans that dependency.

2. **`before_tool_callback` return-value contract for `deny_pr_tool_if_absent`.**
   The *mechanism* (per-node tool filtering, no cross-node tool mutation) is
   grounded in primary ADK source (`base_toolset.py`, `plugin_manager.py`), so it
   stays in the design. The exact return-value contract for denying a call in the
   pinned ADK version was not traced end to end. **Recommendation:** verify
   against `plugin_manager.py` before Phase 2 ships; worst-case failure mode if
   wrong is "tool isn't denied", not a crash, so this does not block Phase 1–2.

3. **Failed-verifier-audit posting policy.** Default: post with an explicit
   low-confidence banner rather than suppress, because the eval methodology
   treats engineer feedback as ground truth — suppressing a flagged report
   removes that signal entirely. Alternative: block posting and route to a
   separate human-review queue. **Recommendation:** banner-and-post as the
   default, but it changes what "engineer feedback" as an eval signal actually
   measures (feedback on a flagged report vs. on a queue-triaged one).

4. **Token budget enforcement wiring.** `TokenBudget.token_limit`/`tokens_used`
   are in the schema, but where `tokens_used` increments, and what routes on
   token-cap-exceeded (the same `budget_exceeded` route as the turn cap, or a
   separate one), is left as a Phase 2 implementation detail. Relevant given the
   "absorb raw token costs" freeze, which makes this the one governance mechanism
   actually bounding that cost.

5. **LiteLLM/Bedrock caching freeze.** Orthogonal to this graph redesign and
   still unresolved — no unfreeze trigger is stated anywhere.

---

## Appendix A — superseded proposal

Kept for the record: this is what the refactor originally proposed, before the
review pipeline ran. Several items below are **wrong** and are retained only so
the rejections in the final design are traceable.

### A.1 Original target architecture

```
                                  [ External Ingestion Event ]
                                             │
          ┌────────────────────┴────────────────────┐
          ▼                                         ▼
[ Test Operator Trigger ]                [ Slack / Manual Trigger ]
  (Single Failure Mode)                       (Full RUN_ID Mode)
          │                                         │
          │                                         ▼
          │                                [ Failure Router Node ]
          │                                   (Groups Errors)
          │                                         │
          └────────────────────┬────────────────────┘
                               │
                               ▼
                  [ Branch Guard (Non-LLM) ]
              (Check PR presence & Set tool flags)
                               │
                               ▼
                [ Bounded Investigator Node ]
              (Sub-Graph: Max 3-5 Tool Turns)
                               │
                               ▼
                   [ Verifier Linter Node ]
                    (Evidence-Claim Check)
                               │
                               ▼
             [ Slack Dispatch & Dormancy Gate ]
          (State saved via ADK Session Service)
                               │
                               ▼
                [ Webhook Resume Handler ]
              (Engineer Requests Deep-Dive)
                               │
                               ▼
                 [ ADK Runner run_async ]
               (Applies state_delta & feedback)
                               │
                               ▼
                [ Targeted Deep-Dive Node ]
               (Max 3 Turns with Feedback Context)
                               │
                               ▼
                   [ Final Output Node ]
```

### A.2 Rejected mechanics, and why

| Original mechanic | Verdict |
| --- | --- |
| `execution_status` enum in `GovernanceState` as the pause/resume state machine | **Rejected.** Duplicates ADK's native `RequestInput`/`resume_inputs` lifecycle and the event trace. Removed from the schema. |
| `SREAgentResumeHandler` calling `runner.run_async(..., state_delta={"governance.execution_status": ...})` | **Broken.** State merges are a flat `dict.update()`; dotted keys create a literal `"governance.execution_status"` key rather than updating the nested dict. Replaced with a `FunctionResponse` answering the pending `adk_request_input` call. |
| Branch guard "programmatically strips PR-fetching tools from downstream nodes" | **Not a real ADK mechanism.** Replaced with a `has_pr` state flag plus a `before_tool_callback` owned by the consuming node. |
| `check_turn_budget()` raising `RuntimeError` on budget exhaustion | **Rejected.** Budget exhaustion is an expected terminal condition, not a crash. Replaced with a `budget_exceeded` route. |
| `investigation_reports: Dict[str, str]` and `verifier_audit: Dict[str, bool]` as parallel dicts in state | **Replaced** by a single `investigation_summaries: Dict[str, InvestigationSummary]`, with the report body in Artifacts and `audit_passed` as a field on the summary. |
| `InMemorySessionService` "with zero changes to graph logic" when swapping to `DatabaseSessionService` | **Partially true**, but the separate-process resume topology makes `ResumabilityConfig(is_resumable=True)` mandatory, which the original never named. |

### A.3 Boundary rules retained from the original

* **Data separation** — keep runtime metadata (`GovernanceState`) separate from
  model context to avoid context clutter and token waste. *(Carried forward.)*
* **Tool docstrings** — Python tool docstrings become the LLM function
  specification, so they must state input/output contracts and operational bounds
  explicitly. *(Carried forward.)*

---

## See also

* [`source-code-comprehension.md`](./source-code-comprehension.md) — how the
  investigator nodes read source code without a clone.
* [`assets/intermediate-design-review.png`](./assets/intermediate-design-review.png)
  — screenshot of the intermediate review that fed this design.
