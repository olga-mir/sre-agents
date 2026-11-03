#!/usr/bin/env bash
# Project resources for a fresh deploy. Idempotent.
#
# Creates what can be created with gcloud before any agent exists:
# APIs, service identities, the completions bucket, the GitHub App private-key
# secret, and the BigQuery dataset. Agent Identity principals do not
# exist until the first deploy, so IAM for those principals is
# agents/e2e-agent/deploy/grant_iam.sh and agents/sre-coordinator/deploy/grant_iam.sh.
#
#   deploy/provision.sh            # everything below
#   deploy/provision.sh bigquery   # dataset and DDL only (task bq:seed)
#
# GitHub App PEM. `all` and `secret` both enable Secret Manager, then
# create the secret when it is absent. Re-running does not add a version
# unless ROTATE_GITHUB_APP_KEY=1.
#   GITHUB_APP_PRIVATE_KEY_FILE=/path/to/app.pem deploy/provision.sh
#   GITHUB_APP_PRIVATE_KEY_FILE=/path/to/app.pem deploy/provision.sh secret
set -euo pipefail
# A disabled API makes gcloud ask "enable and retry?" on stdin and write
# the question to stderr. Discarding stderr looks like a hang. Prompts off:
# that question becomes an error instead of a read.
export CLOUDSDK_CORE_DISABLE_PROMPTS=1

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
  # --billing-project: without it gcloud charges the call to the user-credential
  # quota project (not ${PROJECT}), and fails if Cloud Billing API is off there.
  if ! account="$(gcloud billing projects describe "${PROJECT}" --billing-project="${PROJECT}" --format='value(billingAccountName)')"; then
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
  if gcloud storage buckets describe "gs://${name}" --project="${PROJECT}" >/dev/null; then
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
  if bq show --format=prettyjson --location="${BQ_LOCATION}" "${PROJECT}:${DATASET}" >/dev/null; then
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

# After `gcloud services enable` reports the operation done (it waits; we
# do not pass --async), Secret Manager can still 403 for a short time.
# 180s is the bound on that propagation only, in 15s pauses.
SECRET_API_PROPAGATION_DEADLINE_SECONDS=180
SECRET_API_PROPAGATION_PAUSE_SECONDS=15

# gcloud's text stays in PROBE_OUT / PROBE_ERR. Classifying it with [[ ]]
# under set -x would print that text in the trace. grep the files instead.
# The PEM is never copied into either file by this script.
api_still_disabled() {
  grep -q -e 'has not been used' -e 'it is disabled' -e 'SERVICE_DISABLED' \
    "$PROBE_OUT" "$PROBE_ERR"
}

secret_is_absent() {
  grep -q -e 'NOT_FOUND' -e 'not found' "$PROBE_OUT" "$PROBE_ERR"
}

secret_already_exists() {
  grep -q -e 'ALREADY_EXISTS' -e 'already exists' "$PROBE_OUT" "$PROBE_ERR"
}

show_probe() {
  if [[ -s "$PROBE_OUT" ]]; then
    cat "$PROBE_OUT" >&2
  fi
  if [[ -s "$PROBE_ERR" ]]; then
    cat "$PROBE_ERR" >&2
  fi
}

run_gcloud() {
  : >"$PROBE_OUT"
  : >"$PROBE_ERR"
  # Output goes to the probe files so set -x cannot reprint it. show_probe
  # prints those files once. --data-file stays a path in the trace.
  "$@" >"$PROBE_OUT" 2>"$PROBE_ERR" && GCLOUD_RC=0 || GCLOUD_RC=$?
  return "$GCLOUD_RC"
}

wait_for_secret_api() {
  local remaining pause
  remaining=$((SECRET_API_PROPAGATION_DEADLINE_SECONDS - (SECONDS - SECRET_API_WAIT_STARTED)))
  if (( remaining <= 0 )); then
    echo "secretmanager.googleapis.com still disabled after ${SECRET_API_PROPAGATION_DEADLINE_SECONDS}s." >&2
    exit 1
  fi
  pause="${SECRET_API_PROPAGATION_PAUSE_SECONDS}"
  if (( pause > remaining )); then
    pause="${remaining}"
  fi
  echo "secretmanager.googleapis.com is not accepting calls yet; waiting ${pause}s"
  sleep "${pause}"
}

require_pem_file() {
  [[ -n "${GITHUB_APP_PRIVATE_KEY_FILE:-}" ]] || {
    echo "Secret ${SECRET} does not exist." >&2
    echo "Create a GitHub App, install it on the repos e2e-agent will read," >&2
    echo "and rerun with its private key PEM:" >&2
    echo "  GITHUB_APP_PRIVATE_KEY_FILE=/path/to/app.pem deploy/provision.sh secret" >&2
    echo "Put GITHUB_APP_ID and GITHUB_APP_INSTALLATION_ID in .env." >&2
    echo "This is the app the running agent uses to read the investigated repo." >&2
    exit 1
  }
  [[ -f "${GITHUB_APP_PRIVATE_KEY_FILE}" ]] || {
    echo "GITHUB_APP_PRIVATE_KEY_FILE is not a file: ${GITHUB_APP_PRIVATE_KEY_FILE}" >&2
    exit 1
  }
}

add_secret_version_if_rotating() {
  if [[ -z "${GITHUB_APP_PRIVATE_KEY_FILE:-}" || "${ROTATE_GITHUB_APP_KEY:-}" != "1" ]]; then
    return 0
  fi
  require_pem_file
  # A new version on every run with ROTATE_GITHUB_APP_KEY=1 is the point of that flag.
  gcloud secrets versions add "${SECRET}" \
    --data-file="${GITHUB_APP_PRIVATE_KEY_FILE}" \
    --project="${PROJECT}"
  echo "Added a new version of ${SECRET}"
}

ensure_secret() {
  echo "Enabling secretmanager.googleapis.com on ${PROJECT}"
  gcloud services enable secretmanager.googleapis.com --project="${PROJECT}"
  SECRET_API_WAIT_STARTED=$SECONDS
  PROBE_OUT="$(mktemp)"
  PROBE_ERR="$(mktemp)"
  trap 'rm -f "$PROBE_OUT" "$PROBE_ERR"' EXIT

  while true; do
    if run_gcloud gcloud secrets describe "${SECRET}" --project="${PROJECT}" --format='value(name)'; then
      echo "Secret ${SECRET} exists"
      add_secret_version_if_rotating
      return 0
    fi
    if secret_is_absent; then
      echo "Secret ${SECRET} is absent"
      break
    fi
    show_probe
    if ! api_still_disabled; then
      echo "Could not describe secret ${SECRET}." >&2
      exit 1
    fi
    wait_for_secret_api
  done

  require_pem_file
  echo "Creating secret ${SECRET}"
  while true; do
    # Default replication policy is automatic. --data-file is a path; gcloud reads the PEM.
    if run_gcloud gcloud secrets create "${SECRET}" \
      --data-file="${GITHUB_APP_PRIVATE_KEY_FILE}" \
      --project="${PROJECT}"; then
      show_probe
      echo "Created secret ${SECRET}"
      return 0
    fi
    show_probe
    if secret_already_exists; then
      echo "Secret ${SECRET} exists"
      return 0
    fi
    if ! api_still_disabled; then
      echo "Could not create secret ${SECRET}." >&2
      exit 1
    fi
    wait_for_secret_api
  done
}

step="${1:-all}"
# xtrace prints expanded words, not file contents. The PEM is only named
# as --data-file. gcloud's replies are written to the probe files and
# printed once by show_probe, so the trace does not repeat them.
set -x
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
