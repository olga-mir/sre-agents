"""Session state and the typed findings that flow through the graph.

Schemas follow docs/graph-workflow-with-hitl.md §3.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from typing_extensions import TypedDict

from pydantic import BaseModel, Field

EvidenceKind = Literal[
    "run_metadata", "ci_log", "pr_overview", "pr_diff",
    "commit_patch", "source_file", "ref_search", "test_history",
]


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


class TriggerEvent(BaseModel):
    """Graph input. `repo` and `run_id` are all an investigation needs."""

    trigger_source: Literal["TEST_OPERATOR", "SLACK_MANUAL"] = "TEST_OPERATOR"
    repo: str
    run_id: str
    failure_id: str = "primary"


class FailureGroup(BaseModel):
    failure_id: str
    test_name: str
    error_message: str
    failure_type: Literal[
        "CLUSTER_PROVISION", "GINKGO_TEST", "INFRASTRUCTURE", "UNKNOWN"
    ]


class DeepDiveFeedback(BaseModel):
    """The HITL reply. Gemini Enterprise renders this schema as the form.

    Keep it to these two fields. Anything else placed on `RequestInput.payload`
    is sent as function-call arguments and the Enterprise UI turns those
    arguments into more form fields.
    """

    decision: Literal["finish", "dig_deeper"] = Field(
        title="What next",
        description="finish closes the investigation. "
                    "dig_deeper runs one more pass on the focus below.",
    )
    focus: str = Field(
        default="",
        title="What to look at",
        description="A path, symbol, or question. Leave blank when you finish.",
    )


class EvidenceRef(BaseModel):
    """A durable, replayable pointer to one piece of evidence.

    ONE identity: (kind, key). `key` is always a string - a job id, a path, a
    pattern, a sha - never an int.
    """

    kind: EvidenceKind
    key: str
    pinned_to: str
    filename: str
    byte_len: int = 0
    truncated: bool = False


class BaselineCheck(BaseModel):
    """The 'was this already broken on base?' step.

    Absence on base does NOT prove introduction on its own: an assertion string
    carrying a timestamp, port, UUID or pod name returns zero hits on base AND
    head, and a failed search returns zero hits too. Both read as 'introduced'
    unless present_on_head and search_ok are checked as well.
    """

    literal: str
    search_ok: bool
    truncated: bool = False
    present_on_base: bool = False
    present_on_head: bool = False
    base_hits: list[str] = Field(default_factory=list)
    head_hits: list[str] = Field(default_factory=list)

    @property
    def supports_introduction(self) -> bool:
        return (self.search_ok and not self.truncated
                and self.present_on_head and not self.present_on_base)


Verdict = Literal[
    "INTRODUCED_BY_COMMIT",
    "PRE_EXISTING",
    "INFRA_SUSPECTED",
    "FLAKE_SUSPECTED",
    "INDETERMINATE",
]


class CodeFinding(BaseModel):
    """The investigator's typed output.

    Assertion fields are Optional because the agent's primary job is telling
    infrastructure flakes from real regressions, and an OOM kill or an evicted
    runner has no assertion literal at all.
    """

    failure_id: str = "primary"
    verdict: Verdict = "INDETERMINATE"
    failure_type: Literal[
        "CLUSTER_PROVISION", "GINKGO_TEST", "INFRASTRUCTURE", "UNKNOWN"
    ] = "UNKNOWN"
    failed_test: Optional[str] = None
    failed_job_names: list[str] = Field(default_factory=list)
    assertion_expected: Optional[str] = None
    assertion_actual: Optional[str] = None
    offending_sha: Optional[str] = None
    offending_paths: list[str] = Field(default_factory=list)
    offending_symbols: list[str] = Field(
        default_factory=list,
        description="Symbols named the way the outline names them, e.g. "
                    "'TestReconcile/missing_backend' or '(*Server).handleAuth'.",
    )
    baseline: Optional[BaselineCheck] = None
    flake_signal: Optional[str] = None
    confidence: float = Field(
        default=0.0,
        description="How sure you are, from 0.0 to 1.0. Never above 1.0.",
    )
    one_line_summary: str = ""
    reasoning: str = ""
    evidence: list[EvidenceRef] = Field(default_factory=list)


class InvestigationSummary(BaseModel):
    """`exit_reason` carries what a route would have carried.

    ADK's graph validation rejects two edges sharing a (from, to) pair, so
    'investigation_complete', 'infra_suspected' and 'budget_exceeded' cannot be
    three labelled edges into one verifier, and 'passed'/'failed' cannot be two
    labelled edges into one dormancy gate. The distinction travels on the
    payload instead - which is where the banner reads it from anyway.
    """

    failure_id: str
    confidence: float
    one_line_summary: str
    exit_reason: Literal[
        "investigation_complete", "infra_suspected", "budget_exceeded",
        "deep_dive", "quota_exhausted", "llm_call_limit_exceeded"
    ] = "investigation_complete"
    audit_passed: bool = False
    audit_notes: list[str] = Field(default_factory=list)
    budget: Optional[TokenBudget] = None
    finding: Optional[CodeFinding] = None


class AgentState(TypedDict, total=False):
    governance: GovernanceState
    has_pr: bool
    run_context: Optional[dict]
    failure_groups: list[FailureGroup]
    investigation_summaries: dict[str, Any]
    engineer_feedback: Optional[str]
    dormancy_retry_count: int


def merge_summary(ctx: Any, summary: InvestigationSummary) -> dict[str, Any]:
    """Whole-map write.

    ADK state merges are a flat top-level dict.update()
    (base_session_service.py:226, in_memory_session_service.py:385,
    database_session_service.py:936), so writing
    {'investigation_summaries': {failure_id: s}} REPLACES the whole map and
    silently drops every other failure group. Always read-modify-write.

    Corollary: no tool may write this key. Parallel function calls give two
    writers in one turn and last-writer-wins.
    """
    current = dict(ctx.state.get("investigation_summaries") or {})
    current[summary.failure_id] = summary.model_dump()
    return {"investigation_summaries": current}
