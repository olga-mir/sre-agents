# docs

This repo is a contrived, deliberately small example built for the conference
talk ["Structure Over Prompts: Building an SRE Agent on Gemini Enterprise Agent
Platform"](https://olga-mir.github.io/Public-Speaking/2026.10.03__GDG-Melbourne-DevFest__Structure-Over-Prompts/)
(GDG DevFest Melbourne, 2026-10-03). It exists to demo three things —
**sre-coordinator**, **e2e-agent**, and a BigQuery data agent — in a GEAP
console, not to be a template for a production system. For a comprehensive,
maintained set of ADK patterns and recipes, see
[google/adk-recipes](https://github.com/google/adk-recipes) instead.

This folder was flattened to just what supports that demo. Read in this order:

1. [`graph-workflow-with-hitl.md`](./graph-workflow-with-hitl.md) — the nine-node
   graph and the human-in-the-loop pause.
2. [`geap-adk-streamquery.md`](./geap-adk-streamquery.md) — how Gemini
   Enterprise invokes the agent.
3. [`e2e-agent-investigation-method.md`](./e2e-agent-investigation-method.md) —
   the manual CI-failure investigation the agent's behaviour is modelled on.
4. [`source-code-comprehension.md`](./source-code-comprehension.md) — how the
   investigator reads source with no clone and no working tree.
5. [`graph-implementation-notes.md`](./graph-implementation-notes.md) — what
   the ADK runtime corrected once the graph actually ran (e.g. no two edges
   may share a `(from, to)` pair).
6. [`code-review-guide.md`](./code-review-guide.md) — what a review of this
   code has to look for, and why it differs from reviewing a request/response
   service.
7. [`demo-prompts.md`](./demo-prompts.md) — the demo script: prompts for the
   coordinator, the BigQuery data agent, and direct e2e-agent triggers.
