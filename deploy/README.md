# Deploy

Bash and `gcloud` provision the project. `task deploy` updates the two
Agent Runtime engines. Agent Identity grants run after the first deploy
of each engine, because the principal does not exist before that.

`scripts/` drives the demo (`demo.sh`) and pulls session events.

## Fresh project

You need `gcloud`, `bq`, `python3`, and a project with a billing account.
`gcloud auth login` covers these scripts. `task deploy` uses Application
Default Credentials, so also run `gcloud auth application-default login`.
Owner covers the grants below. Otherwise you need to enable APIs, create
buckets and secrets, deploy Agent Runtime, create a data agent, and set
IAM.

```bash
# 1. APIs, buckets, eval dataset. If the GitHub App secret is missing,
#    this exits and tells you how to rerun with the PEM.
deploy/provision.sh

# 2. Link this repo so e2e-agent deploys from GitHub. See below.
deploy/developer_connect.sh

# 3. Data agent over ci_agent_eval. Copy the printed resource name.
deploy/data_agent.sh

# 4. Deploy. .env needs the variables in the next section.
task deploy
deploy/grant_agent_identity_iam.sh
task deploy AGENT=sre-coordinator
deploy/grant_coordinator_iam.sh

# 5. Register into an existing Gemini Enterprise app.
task publish GE_APP=projects/PROJECT/locations/global/collections/default_collection/engines/APP_ID
```

`deploy/provision.sh bigquery` is what `task bq:seed` calls. Re-running
any of these scripts leaves existing resources in place.

## Two GitHub credentials

| Credential | Used for | Where it lives |
|---|---|---|
| GitHub App you create | Runtime reads of the repo under investigation (`E2E_AGENT_REPO`) | PEM in secret `e2e-agent-github-app-private-key`. `GITHUB_APP_ID` and `GITHUB_APP_INSTALLATION_ID` in `.env`, copied into Agent Runtime by `task deploy`. |
| Developer Connect GitHub App | Fetching this repo at deploy time | OAuth secret and installation id on the Developer Connect connection. |

Local `task investigate` uses `GITHUB_TOKEN` from `task setup`. Agent
Runtime has no `gh` login, so the deployed agent uses the GitHub App.

## Developer Connect

`task deploy` builds e2e-agent from `DC_REPO_LINK` at `DC_REVISION`
(default `main`), not from the working tree. Push first.

`gcloud developer-connect connections create` for GitHub requires the
OAuth token secret version and the Developer Connect installation id.
The console flow (Developer Connect → GitHub) writes that secret. Then:

```bash
DC_CLONE_URI=https://github.com/OWNER/sre-agents.git \
DC_REPO_LINK=sre-agents \
DC_OAUTH_SECRET_VERSION=projects/PROJECT/secrets/NAME/versions/1 \
DC_INSTALLATION_ID=123456 \
deploy/developer_connect.sh
```

`DC_INSTALLATION_ID` is the Developer Connect app's installation, not
`GITHUB_APP_INSTALLATION_ID`. Connection name defaults to `gh-connect`
in `us-central1`, which is what `deploy/deploy_agent.py` reads.

## Still done by hand

The checklist is in the root README: GitHub App, Developer Connect
authorization, the Gemini Enterprise app, and an optional Model Garden
endpoint. `grant_*.sh` also waits until after the first deploy, because
it reads `effectiveIdentity` off the engine.

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
| `developer_connect.sh` | Once per source repo |
| `data_agent.sh` | Once per project |
| `deploy_agent.py` | `task deploy` (Developer Connect source) |
| `deploy_sre_coordinator.py` | `task deploy AGENT=sre-coordinator` (local bundle) |
| `grant_agent_identity_iam.sh` | After the first e2e-agent deploy |
| `grant_coordinator_iam.sh` | After the first coordinator deploy |
| `invoke_agent.py` | `task invoke` |
