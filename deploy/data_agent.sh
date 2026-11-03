#!/usr/bin/env bash
# Create the BigQuery data agent sre-coordinator calls for history
# questions, then register its A2A card in Agent Registry. Idempotent:
# an existing data agent and an existing registry service are left as they are.
#
# gcloud has no dataAgents command. The create body is deploy/data_agent.json.
# This script enables APIs and checks the dataset, then deploy/data_agent.py
# calls the Gemini Data Analytics REST API and Agent Registry services.create.
# https://docs.cloud.google.com/gemini/data-agents/reference/rest/v1beta/projects.locations.dataAgents/create
# https://docs.cloud.google.com/agent-registry/reference/rest/v1alpha/projects.locations.services/create
#
# The data agent id and location come from ENV_INTELLIGENCE_AGENT in the
# environment or the repo .env. DATA_AGENT_ID and DATA_AGENT_LOCATION
# override that. With neither set, the id is environment-intelligence and
# the location is us. The dataset itself stays in BQ_LOCATION
# (us-central1). Agent Registry rejects manual
# registration in multi-regions us and eu, so the service is created in
# DATA_AGENT_REGISTRY_LOCATION (default global). The caller needs
# roles/agentregistry.editor. Gemini Enterprise lists the entry when its
# Agent Gateway registry binding is that same location.
#
# Prints ENV_INTELLIGENCE_AGENT and AGENT_REGISTRY_RESOURCE.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# One KEY=VALUE from a dotenv file. Does not source the file.
dotenv_value() {
  local file="$1" key="$2" line value
  [[ -f "${file}" ]] || return 0
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line%%$'\r'}"
    [[ "${line}" =~ ^[[:space:]]*# ]] && continue
    [[ "${line}" == "${key}="* ]] || continue
    value="${line#"${key}"=}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    if [[ "${value}" == \"*\" ]]; then
      value="${value:1:${#value}-2}"
    fi
    printf '%s' "${value}"
    return 0
  done < "${file}"
}

if [[ -z "${ENV_INTELLIGENCE_AGENT:-}" ]]; then
  ENV_INTELLIGENCE_AGENT="$(dotenv_value "${ROOT}/../.env" ENV_INTELLIGENCE_AGENT)"
fi

PROJECT="${PROJECT:-${GOOGLE_CLOUD_PROJECT:?set PROJECT or GOOGLE_CLOUD_PROJECT}}"
DATASET="${DATASET:-${BQ_DATASET:-ci_agent_eval}}"
DATA_AGENT_REGISTRY_LOCATION="${DATA_AGENT_REGISTRY_LOCATION:-global}"
# urllib.request.urlopen blocks forever when timeout is omitted. data_agent.py
# passes this value. Default 120s.
CURL_MAX_TIME="${CURL_MAX_TIME:-120}"

for bin in gcloud python3 bq; do
  command -v "${bin}" >/dev/null 2>&1 || { echo "required command not on PATH: ${bin}" >&2; exit 1; }
done

gcloud services enable \
  geminidataanalytics.googleapis.com \
  bigquery.googleapis.com \
  agentregistry.googleapis.com \
  --project="${PROJECT}"

BQ_LOCATION="${BQ_LOCATION:-us-central1}"
if ! bq show --format=prettyjson --location="${BQ_LOCATION}" "${PROJECT}:${DATASET}" >/dev/null 2>&1; then
  echo "Dataset ${PROJECT}:${DATASET} does not exist. Run deploy/provision.sh bigquery first." >&2
  exit 1
fi

PROJECT="${PROJECT}" \
DATASET="${DATASET}" \
ENV_INTELLIGENCE_AGENT="${ENV_INTELLIGENCE_AGENT:-}" \
DATA_AGENT_LOCATION="${DATA_AGENT_LOCATION:-}" \
DATA_AGENT_ID="${DATA_AGENT_ID:-}" \
DATA_AGENT_REGISTRY_LOCATION="${DATA_AGENT_REGISTRY_LOCATION}" \
CURL_MAX_TIME="${CURL_MAX_TIME}" \
DATA_AGENT_TOKEN="$(gcloud auth print-access-token)" \
python3 "${ROOT}/data_agent.py"
