# sre-agents

ADK agents on Gemini Enterprise Agent Platform, built for the talk
[Structure Over Prompts](https://olga-mir.github.io/Public-Speaking/2026.10.03__GDG-Melbourne-DevFest__Structure-Over-Prompts/).

It demos three things in a GEAP console: **sre-coordinator**, **e2e-agent**, and a BigQuery data agent. It is a trimmed example, not a production template. For maintained ADK patterns, see [google/adk-recipes](https://github.com/google/adk-recipes).

**e2e-agent** investigates one failed GitHub Actions run. It pins the run to frozen shas, reads logs and source through the GitHub API, verifies the claim, writes the outcome to BigQuery, and pauses for a human. There is no clone.

**sre-coordinator** is the front door. History questions go to the BigQuery data agent. A request to investigate a specific run goes to e2e-agent.

Demo prompts are in [`docs/demo-prompts.md`](docs/demo-prompts.md). Design notes are under [`docs/`](docs/README.md).

## Run

Set `GOOGLE_CLOUD_PROJECT` in `.env` (see `.env.example`). `task --list` shows every target.

```bash
task setup
task test
task investigate                 # RUN=<id> REPO=owner/name
task investigate FEEDBACK="..."  # resume the pause
task debug VIEW=report           # also trace, evidence, triage, logs
task web
```

The default repo is `agent-substrate/substrate`. Each investigation writes `runs/<run_id>/`.

## Warehouse

The graph writes findings to BigQuery dataset `ci_agent_eval`. Details: [`analytics/bq/README.md`](analytics/bq/README.md).

```bash
task bq:iam MEMBER=user:you@example.com
task bq:seed
task bq:seed WHAT=investigations
task bq:query Q=ci_health
```

## Deploy

`task deploy` updates e2e-agent from a Developer Connect checkout of `DC_REPO_LINK` at `DC_REVISION` (default `main`), not from the working tree. Push first. It copies `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, and `GITHUB_APP_PRIVATE_KEY_SECRET` into Agent Runtime. Locally, `task setup` writes a `GITHUB_TOKEN` into `.env`.

```bash
task deploy                      # AGENT=sre-coordinator for that agent
task invoke                      # RUN=<id>
task publish                     # list apps; GE_APP=... to register
```

`task deploy AGENT=sre-coordinator` bundles that agent from local source. Set `ENV_INTELLIGENCE_AGENT` (see `.env.example`). `E2E_AGENT_ENGINE` is optional: unset, the coordinator looks up the runtime named `e2e-agent`.

Gemini Enterprise calls e2e-agent with `:streamQuery`. A local MCP server is `python -m e2e_agent.agent.mcp_entrypoint`. The tool takes TriggerEvent JSON.
