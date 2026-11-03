# Deploy

Each agent is a separate deployment with its own lifecycle: `task deploy:e2e` and `task deploy:coordinator` create or update one Agent Runtime engine each with `agents-cli`, and neither touches the other. What differs per agent lives in `agents/<agent>/deploy/`; the scripts in this directory are generic and take the agent name.

Project and infra provisioning need to happen first before agent deployment, but Agent Identity grants run after the first deploy of each engine, because the principal does not exist before that.

## Fresh project

```bash
# 1. APIs, the completions bucket, the eval dataset, and the GitHub App secret.
GITHUB_APP_PRIVATE_KEY_FILE=/path/to/app.pem deploy/provision.sh

# 2. Data agent over ci_agent_eval, and its A2A card in Agent Registry.
#    The data agent stays in us. The registry service is created in global,
#    because us and eu reject manual registration. Copy the printed data-agent
#    resource name into .env as ENV_INTELLIGENCE_AGENT.
ENV_INTELLIGENCE_AGENT=<from-prev-step> deploy/data_agent.sh

# 3. Deploy each agent, then grant its identity. They are independent: run
#    either alone, in either order (the coordinator only needs e2e-agent
#    deployed before it is asked to investigate). .env needs GITHUB_APP_ID and
#    GITHUB_APP_INSTALLATION_ID for e2e-agent. The pinned runtime envs are
#    agents/<agent>/deploy/runtime.env; deploy does not copy .env.
task deploy:e2e
task iam:e2e                 # agents/e2e-agent/deploy/grant_iam.sh
task deploy:coordinator
task iam:coordinator         # agents/sre-coordinator/deploy/grant_iam.sh

# 4. Register the engines. .env needs GEMINI_ENTERPRISE_APP_ID (the app id).
task publish:e2e
task publish:coordinator
```

`deploy/provision.sh bigquery` is what `task bq:seed` calls. Re-running any of these scripts leaves existing resources in place.

## GitHub credential

Create GitHub App to read the repo which e2e CI runs are investigated by the agent. The repo is configured with `E2E_AGENT_REPO`
PEM in secret `e2e-agent-github-app-private-key`. `GITHUB_APP_ID` and `GITHUB_APP_INSTALLATION_ID` in `.env`. `task deploy:e2e` copies them into Agent Runtime.

Local `task investigate` uses `GITHUB_TOKEN` from `task setup`. Agent Runtime has no `gh` login, so the deployed agent uses the GitHub App. `deploy/stage.sh` never copies `.env` into the build directory, so of the tree before `agents-cli` runs, so that token is not copied into the runtime.

## What agents-cli deploys

Each agent has its own `Dockerfile`, `pyproject.toml`, `requirements.txt` and
`agents-cli-manifest.yaml` under `agents/<agent>/`. `agents-cli` deploys the
project root it runs in, and both images need `libs/agent_serving`, which
lives outside the agent directory. So `deploy/stage.sh` copies the agent
plus `agent_serving/` into a temp directory (no `.env`, no `tests/`, no
other agent) and `deploy/deploy.sh` runs `agents-cli deploy` there.

The image runs `uvicorn agent_serving.fast_api_app:app`, which serves
`AdkApp` on the reasoning-engine routes. The Dockerfile sets
`AGENT_MODULE` (`e2e_agent.agent.agent`, `sre_coordinator.agent`); the
harness has no default, so an image cannot serve the wrong agent. The
coordinator image carries no tree-sitter, GitHub client or GCS upload.

`agents-cli` records a `deployment_metadata.json` naming the engine to
update. `deploy.sh` keeps it at `agents/<agent>/deploy/deployment_metadata.json`
(gitignored, contains resource names) and copies it in and out of the
stage. Delete that file to deploy a new engine instead of updating the
recorded one. **Migrating from the single-project layout:** metadata used
to live at `deploy/metadata/<agent>.json`; `deploy.sh` falls back to it
once if the new file is absent and saves it to the new place, so the first
deploy still updates your existing engine instead of creating a second one.

Display names stay `e2e-agent` and `sre-coordinator`. Region is
`us-central1` unless `AGENT_LOCATION` is set. Both engines run as
Agent Identity (`--agent-identity`).

On an update, `agents-cli` keeps environment variables it does not
resend. The pinned files are the values this repo chooses. A variable
left over from an older deploy stays until you remove it in the
console.

`agents-cli` defaults to scaling to zero (minimum instances 0).

## What stays bash and gcloud

`agents-cli` does not create these:

- APIs, the completions bucket, the GitHub App secret, and the
  BigQuery dataset (`deploy/provision.sh`)
- The `environment-intelligence` data agent and its Agent Registry service (`deploy/data_agent.sh`)
- IAM for the agent principals (`agents/e2e-agent/deploy/grant_iam.sh`,
  `agents/sre-coordinator/deploy/grant_iam.sh`)

`agents-cli` grants `roles/aiplatform.user`,
`roles/serviceusage.serviceUsageConsumer`, `roles/browser`,
`roles/cloudapiregistry.viewer`, `roles/logging.logWriter`, and
`roles/monitoring.metricWriter` on first create. That is not secret
access, BigQuery, the completions bucket, or the coordinator's
data-agent roles.

`task invoke` stays the Python SDK. The body is TriggerEvent JSON.
`agents-cli run` sends a chat string.

Creating the Gemini Enterprise app, and an optional Model Garden
endpoint, stay in the console. The checklist is in the root README.
`grant_iam.sh` also waits until after the first deploy, because it reads
`effectiveIdentity` off the engine.

## After the first deploy

`agents/e2e-agent/deploy/grant_iam.sh` grants the e2e-agent principal
secret access, BigQuery write on `ci_agent_eval`, the completions
bucket, and metrics write. It also grants the Vertex evaluation service
agent `roles/observability.viewAccessor` so online monitors can read
`_AllSpans`.

`agents/sre-coordinator/deploy/grant_iam.sh` grants the coordinator
`roles/aiplatform.user` (so it can `stream_query` e2e-agent),
`roles/geminidataanalytics.dataAgentUser`, `roles/cloudaicompanion.user`,
and BigQuery read on `ci_agent_eval`. The data agent runs queries as
the caller.

## Layout

Generic, in this directory:

| Script | When |
|---|---|
| `provision.sh` | Once per project, and again for the dataset |
| `data_agent.sh` | Once per project |
| `deploy.sh <agent>` | `task deploy:e2e` / `task deploy:coordinator` |
| `stage.sh <agent> <dir>` | Called by `deploy.sh`: agent + `agent_serving`, nothing else |
| `publish.sh <agent>` | `task publish:e2e` / `task publish:coordinator` |
| `lib.sh`, `agent_identity.sh`, `bq_dataset_iam.sh` | Sourced by the scripts above and the grant scripts |
| `invoke_agent.py` | `task invoke` |

Per agent, under `agents/<agent>/deploy/`:

| File | What |
|---|---|
| `deploy.conf` | Name, required env, Gemini Enterprise description |
| `runtime.env` | Pinned production env (AGENTS.md invariant 11) |
| `grant_iam.sh` | After the first deploy of that agent |
| `deployment_metadata.json` | The engine it updates. Gitignored |
