#!/usr/bin/env bash
# Project resources for a fresh deploy. Idempotent.
#
# Creates what can be created with gcloud before any agent exists:
# APIs, service identities, the completions bucket, the GitHub App private-key
# secret, and the BigQuery dataset. Agent Identity principals do not
# exist until the first deploy, so IAM for those principals is
# grant_agent_identity_iam.sh and grant_coordinator_iam.sh.
#
#   deploy/provision.sh            # everything below
#   deploy/provision.sh bigquery   # dataset and DDL only (task bq:seed)
#
# GitHub App PEM, when the secret does not exist yet:
#   GITHUB_APP_PRIVATE_KEY_FILE=/path/to/app.pem deploy/provision.sh secret
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="${PROJECT:-${GOOGLE_CLOUD_PROJECT:?set PROJECT or GOOGLE_CLOUD_PROJECT}}"
LOCATION="${LOCATION:-us-central1}"
BQ_LOCATION="${BQ_LOCATION:-us-central1}"
DATASET="${DATASET:-${BQ_DATASET:-ci_agent_eval}}"
COMPLETIONS_BUCKET="${COMPLETIONS_BUCKET:-${PROJECT}-e2e-agent-completions}"
SECRET="${GITHUB_APP_PRIVATE_KEY_SECRET:-e2e-agent-github-app-private-key}"

for bin in gcloud bq python3; do
  command -v "${bin}" >/dev/null 2>&1 || {
    echo "required command not on PATH: ${bin}" >&2
    exit 1
  }
done

require_billing() {
  local account
  if ! account="$(gcloud billing projects describe "${PROJECT}" --format='value(billingAccountName)')"; then
    echo "Could not read billing for ${PROJECT}." >&2
    exit 1
  fi
  if [[ -z "${account}" ]]; then
    echo "Link a billing account before provisioning:" >&2
    echo "  gcloud billing projects link ${PROJECT} --billing-account=ACCOUNT_ID" >&2
    exit 1
  fi
  echo "Billing: ${account}"
}

enable_apis() {
  # Agent Runtime setup:
  #   https://docs.cloud.google.com/gemini-enterprise-agent-platform/build/runtime/setup
  # Agent Identity also requires agentidentity, agentregistry, and apphub:
  #   https://docs.cloud.google.com/iam/docs/create-and-deploy-agent
  # Data agents require BigQuery, Gemini Data Analytics, Gemini for
  # Google Cloud, and Data Catalog (Knowledge Catalog):
  #   https://docs.cloud.google.com/bigquery/docs/create-data-agents
  echo "Enabling APIs on ${PROJECT}"
  gcloud services enable \
    aiplatform.googleapis.com \
    agentidentity.googleapis.com \
    agentregistry.googleapis.com \
    apphub.googleapis.com \
    secretmanager.googleapis.com \
    storage.googleapis.com \
    logging.googleapis.com \
    monitoring.googleapis.com \
    telemetry.googleapis.com \
    cloudtrace.googleapis.com \
    observability.googleapis.com \
    cloudresourcemanager.googleapis.com \
    iam.googleapis.com \
    bigquery.googleapis.com \
    geminidataanalytics.googleapis.com \
    cloudaicompanion.googleapis.com \
    datacatalog.googleapis.com \
    discoveryengine.googleapis.com \
    --project="${PROJECT}"

  # Creates the service agents so later IAM bindings have a principal.
  # The reasoning-engine agent (gcp-sa-aiplatform-re) appears on first
  # Agent Runtime deploy; the identity create below is the Vertex AI
  # service agent.
  gcloud beta services identity create \
    --service=aiplatform.googleapis.com \
    --project="${PROJECT}"
}

ensure_bucket() {
  local name="$1"
  if gcloud storage buckets describe "gs://${name}" --project="${PROJECT}" >/dev/null 2>&1; then
    echo "Bucket gs://${name} exists"
    return 0
  fi
  echo "Creating gs://${name}"
  gcloud storage buckets create "gs://${name}" \
    --project="${PROJECT}" \
    --location="${LOCATION}" \
    --uniform-bucket-level-access \
    --public-access-prevention
}

ensure_buckets() {
  # Completions for the GenAI upload hook. Agent Engine builds the
  # container itself, so there is no staging bucket to create.
  ensure_bucket "${COMPLETIONS_BUCKET}"
}

ensure_dataset() {
  gcloud services enable bigquery.googleapis.com --project="${PROJECT}" --quiet
  if bq show --format=prettyjson --location="${BQ_LOCATION}" "${PROJECT}:${DATASET}" >/dev/null 2>&1; then
    echo "Dataset ${PROJECT}:${DATASET} exists"
  else
    echo "Creating dataset ${PROJECT}:${DATASET}"
    bq --location="${BQ_LOCATION}" --project_id="${PROJECT}" mk --dataset \
      --description "CI-agent eval: GitHub inputs, agent outputs, outcomes" \
      "${PROJECT}:${DATASET}"
  fi
  # ddl.sql pins location us-central1 in the schema OPTIONS clause.
  # BQ_LOCATION must match that, or the CREATE SCHEMA conflicts.
  sed "s/PROJECT_ID/${PROJECT}/g; s/DATASET_ID/${DATASET}/g" "${ROOT}/analytics/bq/ddl.sql" \
    | bq query --use_legacy_sql=false --project_id="${PROJECT}" --location="${BQ_LOCATION}" --quiet
  echo "Applied analytics/bq/ddl.sql to ${PROJECT}:${DATASET}"
}

ensure_secret() {
  if gcloud secrets describe "${SECRET}" --project="${PROJECT}" >/dev/null 2>&1; then
    echo "Secret ${SECRET} exists"
    if [[ -n "${GITHUB_APP_PRIVATE_KEY_FILE:-}" && "${ROTATE_GITHUB_APP_KEY:-}" == "1" ]]; then
      [[ -f "${GITHUB_APP_PRIVATE_KEY_FILE}" ]] || {
        echo "GITHUB_APP_PRIVATE_KEY_FILE is not a file: ${GITHUB_APP_PRIVATE_KEY_FILE}" >&2
        exit 1
      }
      gcloud secrets versions add "${SECRET}" \
        --data-file="${GITHUB_APP_PRIVATE_KEY_FILE}" \
        --project="${PROJECT}"
      echo "Added a new version of ${SECRET}"
    fi
    return 0
  fi
  if [[ -z "${GITHUB_APP_PRIVATE_KEY_FILE:-}" ]]; then
    echo "Secret ${SECRET} does not exist." >&2
    echo "Create a GitHub App, install it on the repos e2e-agent will read," >&2
    echo "and rerun with its private key PEM:" >&2
    echo "  GITHUB_APP_PRIVATE_KEY_FILE=/path/to/app.pem deploy/provision.sh secret" >&2
    echo "Put GITHUB_APP_ID and GITHUB_APP_INSTALLATION_ID in .env." >&2
    echo "This is the app the running agent uses to read the investigated repo." >&2
    exit 1
  fi
  [[ -f "${GITHUB_APP_PRIVATE_KEY_FILE}" ]] || {
    echo "GITHUB_APP_PRIVATE_KEY_FILE is not a file: ${GITHUB_APP_PRIVATE_KEY_FILE}" >&2
    exit 1
  }
  # Default replication policy is automatic.
  echo "Creating secret ${SECRET}"
  gcloud secrets create "${SECRET}" \
    --data-file="${GITHUB_APP_PRIVATE_KEY_FILE}" \
    --project="${PROJECT}"
}

step="${1:-all}"
case "${step}" in
  all)
    require_billing
    enable_apis
    ensure_buckets
    ensure_dataset
    ensure_secret
    ;;
  apis) enable_apis ;;
  buckets) ensure_buckets ;;
  bigquery) ensure_dataset ;;
  secret) ensure_secret ;;
  *)
    echo "usage: deploy/provision.sh [all|apis|buckets|bigquery|secret]" >&2
    exit 1
    ;;
esac
