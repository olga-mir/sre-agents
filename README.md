# sre-agents

ADK agents on Gemini Enterprise Agent Platform for SRE workflows.

This is a contrived, trimmed-down example built for the conference talk
["Structure Over Prompts"](https://olga-mir.github.io/Public-Speaking/2026.10.03__GDG-Melbourne-DevFest__Structure-Over-Prompts/)
— it exists to demo three things (**sre-coordinator**, **e2e-agent**, and a
BigQuery data agent) in a GEAP console, not to be a template for a production
system. For a comprehensive, maintained set of ADK patterns and recipes, see
[google/adk-recipes](https://github.com/google/adk-recipes) instead.

**e2e-agent** investigates a failed GitHub Actions run. It pins the run to frozen shas, reads the workflow output and the source through the GitHub API, verifies the resulting claim, writes the outcome to BigQuery, and pauses for a human. There is no clone and no working tree. Every source read goes through the GitHub API pinned to a sha.

**sre-coordinator** is the front door. Questions about history go to a BigQuery data agent (environment-intelligence). A request to investigate a specific run goes to e2e-agent.

## Layout

```
src/e2e_agent/
  agent/          # the ADK graph: nodes, state, prompts, local runner,
                  # engine_entrypoint.py (AdkApp), mcp_entrypoint.py,
                  # a2a_app.py (not the deploy entrypoint)
  code/           # tree-sitter Go outline, failure-address parsing
  github/         # GitHub API reads: contents, compare, blame, Actions
  tools/          # code and log tools, plus their ADK bindings
  run_context.py  # pin a workflow run to frozen shas
  cli.py          # local harness
  analytics/      # BigQuery row mappers
agents/
  e2e_agent/       # ADK loader entry for e2e-agent
  sre_coordinator/
deploy/
  deploy_agent.py            # idempotent Developer Connect deploy of e2e-agent
  deploy_sre_coordinator.py  # local-source deploy of sre-coordinator
  invoke_agent.py            # stream_query helper
  grant_agent_identity_iam.sh
tests/
analytics/bq/     # DDL, schemas, canned queries
```

Design notes live under `docs/`. [`docs/demo-prompts.md`](docs/demo-prompts.md) is the demo script: prompts for the coordinator, the BigQuery data agent, and direct e2e-agent triggers.

## Running it

Set `GOOGLE_CLOUD_PROJECT` in `.env` (see `.env.example`). `task --list` shows every target.

```bash
task setup                       # venv, deps, .env, grammar check
task test                        # unit tests, no network and no model

task investigate                 # the whole graph on a failed run, then pauses
task investigate FEEDBACK="..."  # resume a pause
task debug VIEW=triage          # the deterministic path, with token accounting
```

`RUN=<id>` picks a GitHub Actions run. `REPO=` picks the repo. The default repo is `agent-substrate/substrate`.

### Inspecting a run

Each `task investigate` writes `runs/<run_id>/`:

```bash
task debug VIEW=report    # the report a human would have been posted
task debug VIEW=trace     # every tool call the agent made, in order
task debug VIEW=evidence  # the bytes it actually read, listed by path
task web                  # the ADK dev UI
```

### Eval warehouse (BigQuery)

Structured inputs, outputs, and outcomes live in `ci_agent_eval`. The graph writes findings and the files it opened through a deterministic `record_outcome` node. Details: [`analytics/bq/README.md`](analytics/bq/README.md).

```bash
task bq:iam MEMBER=user:you@example.com   # creates the dataset if it is missing
task bq:seed                              # latest successful non-scan Actions runs
task bq:seed WHAT=investigations          # local runs/<id>/ dumps
task bq:query Q=ci_health
```

### Deploying

`task deploy` updates e2e-agent in place. It builds from a Developer Connect checkout of `DC_REPO_LINK` at `DC_REVISION` (default `main`), not from the working tree. Push first.

`task deploy` reads `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, and `GITHUB_APP_PRIVATE_KEY_SECRET` from the environment and copies them into Agent Runtime `env_vars`. Locally, `task setup` writes a `GITHUB_TOKEN` into `.env`. The deployed agent has no `gh` CLI.

```bash
task deploy                            # AGENT=sre-coordinator for that agent
task invoke                            # RUN=<id> REPO=<repo>
task deployments
task debug VIEW=logs
```

`task deploy AGENT=sre-coordinator` updates sre-coordinator from a local source bundle. Set `ENV_INTELLIGENCE_AGENT` first (see `.env.example`). `E2E_AGENT_ENGINE` is optional: unset, the coordinator looks up the Agent Runtime named `e2e-agent`.

### Local MCP server

```bash
claude mcp add e2e-agent -- .venv/bin/python -m e2e_agent.agent.mcp_entrypoint
```

The tool takes `TriggerEvent` JSON, for example `{"trigger_source": "TEST_OPERATOR", "repo": "owner/repo", "run_id": "123"}`.

### Gemini Enterprise

Gemini Enterprise invokes e2e-agent with `:streamQuery`.

```bash
task publish   # list Gemini Enterprise apps
task publish GE_APP=projects/YOUR_PROJECT/locations/global/collections/default_collection/engines/APP_ID
```

`ENGINE=` overrides the e2e-agent lookup. Needs `agents-cli` (`uv tool install google-agents-cli`).

## Reading order

1. [`docs/graph-workflow-with-hitl.md`](docs/graph-workflow-with-hitl.md) — the graph and the human pause.
2. [`docs/geap-adk-streamquery.md`](docs/geap-adk-streamquery.md) — how Gemini Enterprise invokes it.
3. [`docs/e2e-agent-investigation-method.md`](docs/e2e-agent-investigation-method.md) — the investigation the agent performs.
4. [`docs/source-code-comprehension.md`](docs/source-code-comprehension.md) — reading source without a clone.
5. [`docs/graph-implementation-notes.md`](docs/graph-implementation-notes.md) — what the ADK runtime corrected once the graph actually ran.
6. [`docs/code-review-guide.md`](docs/code-review-guide.md) — what a review of this code has to look for.
