# Building the Graph: What the ADK Runtime Corrected

**Status:** active. Implementation record for the graph in `src/ci_agent/agent/`,
built against **ADK 2.8.0** and run end to end against real failed runs in
[`agent-substrate/substrate`](https://github.com/agent-substrate/substrate).

**Relates to:** the graph shape in
[`graph-workflow-with-hitl.md`](./graph-workflow-with-hitl.md), and the tool
layer in [`source-code-comprehension.md`](./source-code-comprehension.md).

Everything here was found by making the design run. Where it contradicts an
earlier design-review round (trimmed from this copy of the repo), the runtime
is right.

---

## 1. Converging routes cannot be labelled edges

`final-design.md` §4 wires three investigator exits and both audit outcomes into
single successors:

```python
(investigator_loop, {"investigation_complete": verifier_linter,
                     "infra_suspected": verifier_linter,
                     "budget_exceeded": verifier_linter}),
(verifier_linter, {"passed": dormancy_gate, "failed": dormancy_gate}),
```

**That graph does not compile.** `Workflow.model_post_init` runs
`Graph.validate_graph()`, which raises on two edges sharing a `(from, to)` pair:

```
Graph validation failed. Duplicate edge found: from=investigator_loop, to=verifier_linter
```

The same rule kills `{"TEST_OPERATOR": pin, "SLACK_MANUAL": pin}` on
`trigger_router`.

**A route is a branch, not a label.** When branches converge, the distinction has
to travel on the payload:

- `trigger_router` emits one `valid` route; the trigger source is re-emitted as a
  route by `pin_run_context`, which is where the paths genuinely diverge.
- `investigator_loop` emits a plain edge and carries `exit_reason`
  (`investigation_complete` / `infra_suspected` / `budget_exceeded`) on
  `InvestigationSummary`.
- `verifier_linter` emits a plain edge and carries `audit_passed` + `audit_notes`.

Nothing is lost. The review's actual requirement — "a failed audit is neither
silently dropped nor posted indistinguishably from a verified one" — was always
served by the banner, which reads those fields. The two edges were decoration.

## 2. `Event` carries route and state on `actions`

`Event(route=..., state=...)` is the constructor, but the values land on
`event.actions.route` and `event.actions.state_delta`. Tests that assert on
`event.route` fail with `AttributeError`, which reads like a wiring bug and is
not one.

## 3. `output_schema` and `tools` compose in ADK 2.8

`llm_agent.py:414-417`: tools are exposed during the thought loop and structure
is enforced only on the final output. So the investigator is **one** `LlmAgent`
with twelve tools and `output_schema=CodeFinding`, not an agent plus a separate
extraction pass. `ToolContext` is also just an alias of `Context`
(`tools/tool_context.py:26`).

## 4. `ctx.run_node` is how a `FunctionNode` runs an LLM turn

Open tradeoff 1 chose (a), the `FunctionNode` self-loop, to keep the routed
budget exit. The missing mechanism was `await ctx.run_node(agent, node_input=...)`:
the loop stays ordinary Python — `while budget["current_turn"] < max_turns` — and
budget exhaustion stays a payload value rather than a raised
`LlmCallsLimitExceededError`. The calling node must be `rerun_on_resume=True`;
ADK raises immediately otherwise.

## 5. `failure_router` is deterministic here, not an LLM classifier

**Deviation from the shipped design, flagged for the lead.** The log parser
already yields exact test names, packages and assertion lines, so grouping is a
`groupby`, not a judgement — and an LLM classifier in this position would
paraphrase the literals the method forbids paraphrasing. One group per failing
test, plus one per failing job that names no test (the infrastructure case).
Swapping an `LlmAgent` back in is a one-line edge change if the contract requires
it.

---

## What the live runs showed

Run 33259998849 (`agent-substrate/substrate`, PR #1080), full graph on Vertex:

**The tool path was the designed one, unprompted beyond the instruction:**
`list_failed_jobs` → `list_failure_addresses` → `read_log_window` →
`list_changed_files` → `outline_file` → `read_symbol` → `changed_symbols` →
`blame_lines`. No whole file entered context.

**The verifier did its job.** The investigator returned
`verdict=INTRODUCED_BY_COMMIT, confidence=0.9` with `baseline=null`; the
deterministic check refused it — *"introduction claim with no baseline check"* —
and the report went to the human under a LOW CONFIDENCE banner. That is the
design's stated safe failure mode, working on the first real run, and it is why
`search_ref`/`BaselineCheck` is the next thing to build.

**The model put 5.0 in a 0.0–1.0 field.** Clamping silently would be a lie about
the one number a human skims, so the clamp records an audit note and the banner
carries it.

**HITL round-trip works.** Paused at `dormancy_gate` with `interrupt_id
=deep_dive_request_0`; an engineer reply ("the listen-address change is a red
herring — look at resource exhaustion") resumed into `targeted_deep_dive`, which
re-investigated and returned `FLAKE_SUSPECTED`, correctly marked
*"deep-dive result has not been re-verified"*.

---

## Inspecting a run

`InMemoryRunner` keeps artifacts in memory, so without a dump the evidence a run
read disappears with the process — which makes a postmortem impossible and makes
the verifier's referential check unauditable by a human. `ci-agent investigate
--out runs` writes, per run:

| File | What it answers |
|---|---|
| `report.md` | what the engineer would have been posted, banner included |
| `summaries.json` | the full `InvestigationSummary`, incl. `audit_notes` |
| `trace.jsonl` | every tool call in order, with arguments |
| `evidence/ev/{run_id}/{kind}/{sha12}/{path}` | the bytes the run actually read |

The evidence tree mirrors the artifact naming exactly, so it stays greppable by
path. A real run's tree shows `workerpool_apply.go` fetched at **both**
`88f8259a1293` and `f936206dd544` — the model comparing head against base by
address, which is the behaviour §3's "address by name, not by line" was written
to enable.

`task web` (`adk web agents`) serves the ADK dev UI over the same graph for
event-trace and state inspection.

---

## Not yet built

| Piece | Why it is not here |
|---|---|
| `search_ref` + `BaselineCheck` | Phase 2. Until it lands, no investigation can produce a *verified* `INTRODUCED_BY_COMMIT` — every such report carries a LOW CONFIDENCE banner. |
| `get_test_history` | Phase 3. `FLAKE_SUSPECTED` is currently the model's inference, not a measured failure rate on base. |
| Slack sink + idempotency ledger | `dormancy_gate` yields `RequestInput`; the post is the runner's job. The `interrupt_id`-keyed idem key is specified but has no ledger yet. |
| Artifact retention / redaction | Phase 4. Logs are durable by default with no TTL or classification. |
| GEAP deployment | Deployed to Agent Runtime in `YOUR_PROJECT`. Live queries still need a clean E2E after the GitHub 404-hardening. |
