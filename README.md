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

Project setup is bash and gcloud. Script order and IAM details: [`deploy/README.md`](deploy/README.md).

Do these three things by hand. The scripts cannot do them.

1. **GitHub App for the running agent.** Create one at [github.com/settings/apps](https://github.com/settings/apps) and install it on the repo e2e-agent will read (`E2E_AGENT_REPO`, default `agent-substrate/substrate`). Grant read on Actions, Contents, and Pull requests. Put the App ID and the installation ID in `.env` as `GITHUB_APP_ID` and `GITHUB_APP_INSTALLATION_ID`. Generate a private key and upload it:

   ```bash
   GITHUB_APP_PRIVATE_KEY_FILE=/path/to/app.pem deploy/provision.sh secret
   ```

   That secret is `e2e-agent-github-app-private-key`. This app is what the deployed agent uses. Local `task investigate` still uses the `GITHUB_TOKEN` that `task setup` writes.

2. **Gemini Enterprise app.** Create the app in the console if the project does not have one. `task publish` with no `GE_APP` lists apps. Registration is:

   ```bash
   task publish GE_APP=projects/PROJECT/locations/global/collections/default_collection/engines/APP_ID
   ```

3. **Model Garden, only if you want Gemma.** `model-garden/` deploys a GPU endpoint and bills while the replica is up. e2e-agent stays on `gemini-3.7-flash` unless you point it at an endpoint that has passed `model-garden/smoke.sh`. See [`model-garden/README.md`](model-garden/README.md).

`task deploy` needs `agents-cli` on `PATH` (`uv tool install google-agents-cli`). It uploads the working tree and Agent Engine builds `Dockerfile`. You deploy what is checked out locally.

Then:

```bash
gcloud auth login
gcloud auth application-default login
deploy/provision.sh
deploy/data_agent.sh            # prints ENV_INTELLIGENCE_AGENT; put it in .env
task deploy
deploy/grant_agent_identity_iam.sh
task deploy AGENT=sre-coordinator
deploy/grant_coordinator_iam.sh
task invoke                     # RUN=<id>
```

Run the grant scripts after the first deploy of each engine. The agent principal does not exist before that. `agents-cli` grants a baseline role set on first create. Secret access, BigQuery, the completions bucket, and the coordinator's data-agent roles still come from those scripts. `task deploy AGENT=sre-coordinator` needs `ENV_INTELLIGENCE_AGENT`. `E2E_AGENT_ENGINE` is optional: unset, the coordinator looks up the runtime named `e2e-agent`.

Gemini Enterprise calls e2e-agent with `:streamQuery`. A local MCP server is `python -m e2e_agent.agent.mcp_entrypoint`. The tool takes TriggerEvent JSON.
