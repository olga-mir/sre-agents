# Deploy

`task deploy` updates the two Agent Runtime engines with `agents-cli`.
Bash and `gcloud` provision the project resources the CLI does not
create. Agent Identity grants run after the first deploy of each
engine, because the principal does not exist before that.

`scripts/` drives the demo (`demo.sh`) and pulls session events.

## Fresh project

You need `gcloud`, `bq`, `python3`, `agents-cli`, and a project with a
billing account. `gcloud auth login` covers the bash scripts. `task
deploy` and `task invoke` use Application Default Credentials, so also
run `gcloud auth application-default login`. Owner covers the grants
below. Otherwise you need to enable APIs, create buckets and secrets,
deploy Agent Runtime, create a data agent, and set IAM.

```bash
# 1. APIs, the completions bucket, the eval dataset, and the GitHub App
#    secret. If the secret is missing, this exits and tells you how to
#    rerun with the PEM.
deploy/provision.sh

# 2. Data agent over ci_agent_eval. Copy the printed resource name
#    into .env as ENV_INTELLIGENCE_AGENT.
deploy/data_agent.sh

# 3. Deploy the working tree. .env needs GITHUB_APP_ID and
#    GITHUB_APP_INSTALLATION_ID. The pinned runtime env is
#    deploy/e2e-agent.env; task deploy does not copy .env.
task deploy
deploy/grant_agent_identity_iam.sh
task deploy AGENT=sre-coordinator
deploy/grant_coordinator_iam.sh

# 4. Register into an existing Gemini Enterprise app.
task publish GE_APP=projects/PROJECT/locations/global/collections/default_collection/engines/APP_ID
```

`deploy/provision.sh bigquery` is what `task bq:seed` calls. Re-running
any of these scripts leaves existing resources in place.

## GitHub credential

| Credential | Used for | Where it lives |
|---|---|---|
| GitHub App you create | Runtime reads of the repo under investigation (`E2E_AGENT_REPO`) | PEM in secret `e2e-agent-github-app-private-key`. `GITHUB_APP_ID` and `GITHUB_APP_INSTALLATION_ID` in `.env`. `task deploy` copies those two into Agent Runtime. |

Local `task investigate` uses `GITHUB_TOKEN` from `task setup`. Agent
Runtime has no `gh` login, so the deployed agent uses the GitHub App.
`task deploy` moves `.env` out of the tree before `agents-cli` runs, so
that token is not copied into the runtime.

## What agents-cli deploys

Both engines share one `Dockerfile`. `agents-cli` uploads the working
tree (honoring `.gitignore`) and Agent Engine builds the image. The
process is `uvicorn app.fast_api_app:app`. It serves `AdkApp` on the
reasoning-engine routes. `SRE_AGENT_MODULE` selects the graph:
`app.agent` for e2e-agent, `agents.sre_coordinator.runtime` for the
coordinator.

`agents-cli` records one `deployment_metadata.json`. `deploy/deploy.sh`
keeps a copy per engine under `deploy/metadata/` so the two deploys
update their own engines. Those files are gitignored and contain
resource names. Delete `deploy/metadata/e2e-agent.json` to deploy a new
e2e-agent engine instead of updating the recorded one. Same for
`sre-coordinator.json`.

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
- The `environment-intelligence` data agent (`deploy/data_agent.sh`)
- IAM for the agent principals (`deploy/grant_agent_identity_iam.sh`,
  `deploy/grant_coordinator_iam.sh`)

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
`grant_*.sh` also waits until after the first deploy, because it reads
`effectiveIdentity` off the engine.

## After the first deploy

`deploy/grant_agent_identity_iam.sh` grants the e2e-agent principal
secret access, BigQuery write on `ci_agent_eval`, the completions
bucket, and metrics write. It also grants the Vertex evaluation service
agent `roles/observability.viewAccessor` so online monitors can read
`_AllSpans`.

`deploy/grant_coordinator_iam.sh` grants the coordinator
`roles/aiplatform.user` (so it can `stream_query` e2e-agent),
`roles/geminidataanalytics.dataAgentUser`, `roles/cloudaicompanion.user`,
and BigQuery read on `ci_agent_eval`. The data agent runs queries as
the caller.

## Layout

| Script | When |
|---|---|
| `provision.sh` | Once per project, and again for the dataset |
| `data_agent.sh` | Once per project |
| `e2e-agent.env` | Pinned runtime env for e2e-agent |
| `sre-coordinator.env` | Pinned runtime env for the coordinator |
| `deploy.sh` | `task deploy` (`agents-cli`) |
| `grant_agent_identity_iam.sh` | After the first e2e-agent deploy |
| `grant_coordinator_iam.sh` | After the first coordinator deploy |
| `invoke_agent.py` | `task invoke` |
