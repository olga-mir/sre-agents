"""The graph.

Twelve nodes: pin before branching, one bounded investigator with a routed
budget exit, a deterministic verifier, a warehouse write, and one HITL pause.
"""

from __future__ import annotations

from google.adk import Workflow
from google.adk.apps import App, ResumabilityConfig

from .nodes import (
    branch_guard, dormancy_gate, failure_router, final_output, investigator_loop,
    pin_error, pin_run_context_node, record_outcome_node, targeted_deep_dive,
    trigger_error, trigger_router, verifier_linter,
)
from .state import TriggerEvent

root_agent = Workflow(
    name="sre_investigation",
    input_schema=TriggerEvent,
    edges=[
        ("START", trigger_router),
        (trigger_router, {
            "valid": pin_run_context_node,
            "invalid_trigger": trigger_error,
        }),
        (pin_run_context_node, {
            "TEST_OPERATOR": branch_guard,
            "SLACK_MANUAL": failure_router,
            "no_failed_jobs": pin_error,
        }),
        (failure_router, branch_guard),
        (branch_guard, investigator_loop),
        # All three investigator exits and both audit outcomes converge, so
        # these are plain edges: ADK rejects two edges sharing a (from, to)
        # pair. `exit_reason` and `audit_passed` on the payload carry what the
        # route labels would have carried, and the banner reads them.
        (investigator_loop, verifier_linter),
        (verifier_linter, record_outcome_node),
        (record_outcome_node, {
            "await_human": dormancy_gate,
            "done": final_output,
        }),
        (dormancy_gate, {"invalid_feedback": dormancy_gate,
                         "resume_valid": targeted_deep_dive,
                         "finish": record_outcome_node}),
        (targeted_deep_dive, record_outcome_node),
    ],
)

app = App(
    name="e2e_agent",
    root_agent=root_agent,
    resumability_config=ResumabilityConfig(is_resumable=True),
    # No events_compaction_config: an LLM summarizer over the investigator's
    # tool responses would paraphrase the exact assertion literals the method
    # forbids paraphrasing, and every tool return is already capped.
)

