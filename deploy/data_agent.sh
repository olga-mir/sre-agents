#!/usr/bin/env bash
# Create the BigQuery data agent sre-coordinator calls for history
# questions. Idempotent: an existing agent is left as it is.
#
# gcloud has no dataAgents command. The create body is deploy/data_agent.json.
# This script enables APIs and checks the dataset, then deploy/data_agent.py
# calls the Gemini Data Analytics REST API.
# https://docs.cloud.google.com/gemini/data-agents/reference/rest/v1beta/projects.locations.dataAgents/create
#
# Location is `us`, matching .env.example. The dataset itself stays
# in BQ_LOCATION (us-central1).
#
# Prints the resource name for ENV_INTELLIGENCE_AGENT.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="${PROJECT:-${GOOGLE_CLOUD_PROJECT:?set PROJECT or GOOGLE_CLOUD_PROJECT}}"
DATASET="${DATASET:-${BQ_DATASET:-ci_agent_eval}}"
DATA_AGENT_LOCATION="${DATA_AGENT_LOCATION:-us}"
DATA_AGENT_ID="${DATA_AGENT_ID:-environment-intelligence}"
# urllib.request.urlopen blocks forever when timeout is omitted. data_agent.py
# passes this value. Default 120s.
CURL_MAX_TIME="${CURL_MAX_TIME:-120}"

for bin in gcloud python3 bq; do
  command -v "${bin}" >/dev/null 2>&1 || { echo "required command not on PATH: ${bin}" >&2; exit 1; }
done

gcloud services enable \
  geminidataanalytics.googleapis.com \
  bigquery.googleapis.com \
  --project="${PROJECT}"

BQ_LOCATION="${BQ_LOCATION:-us-central1}"
if ! bq show --format=prettyjson --location="${BQ_LOCATION}" "${PROJECT}:${DATASET}" >/dev/null 2>&1; then
  echo "Dataset ${PROJECT}:${DATASET} does not exist. Run deploy/provision.sh bigquery first." >&2
  exit 1
fi

PROJECT="${PROJECT}" \
DATASET="${DATASET}" \
DATA_AGENT_LOCATION="${DATA_AGENT_LOCATION}" \
DATA_AGENT_ID="${DATA_AGENT_ID}" \
CURL_MAX_TIME="${CURL_MAX_TIME}" \
DATA_AGENT_TOKEN="$(gcloud auth print-access-token)" \
python3 "${ROOT}/data_agent.py"
