#!/usr/bin/env bash
# Create the BigQuery data agent sre-coordinator calls for history
# questions. Idempotent: an existing agent is left as it is.
#
# gcloud has no dataAgents command. This uses the Gemini Data Analytics
# REST API with a token from gcloud.
# https://docs.cloud.google.com/gemini/data-agents/reference/rest/v1beta/projects.locations.dataAgents/create
#
# Location is `us`, matching .env.example. The coordinator talks to
# geminidataanalytics.us.rep.googleapis.com. The dataset itself stays
# in BQ_LOCATION (us-central1).
#
# Prints the resource name for ENV_INTELLIGENCE_AGENT.
set -euo pipefail

PROJECT="${PROJECT:-${GOOGLE_CLOUD_PROJECT:?set PROJECT or GOOGLE_CLOUD_PROJECT}}"
DATASET="${DATASET:-${BQ_DATASET:-ci_agent_eval}}"
DATA_AGENT_LOCATION="${DATA_AGENT_LOCATION:-us}"
DATA_AGENT_ID="${DATA_AGENT_ID:-environment-intelligence}"
# curl with no --max-time waits forever.
CURL_MAX_TIME="${CURL_MAX_TIME:-120}"

for bin in gcloud python3 curl bq; do
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

PARENT="projects/${PROJECT}/locations/${DATA_AGENT_LOCATION}"
NAME="${PARENT}/dataAgents/${DATA_AGENT_ID}"
HOST="https://geminidataanalytics.googleapis.com/v1beta"
TOKEN="$(gcloud auth print-access-token)"

tmp="$(mktemp)"
body="$(mktemp)"
trap 'rm -f "${tmp}" "${body}"' EXIT

http_code="$(
  curl -sS --max-time "${CURL_MAX_TIME}" -o "${tmp}" -w '%{http_code}' \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "x-goog-user-project: ${PROJECT}" \
    "${HOST}/${NAME}"
)"

if [[ "${http_code}" == "200" ]]; then
  echo "Data agent already exists: ${NAME}"
  echo "ENV_INTELLIGENCE_AGENT=${NAME}"
  exit 0
fi
if [[ "${http_code}" != "404" ]]; then
  echo "GET ${NAME} returned HTTP ${http_code}" >&2
  cat "${tmp}" >&2
  exit 1
fi

python3 - "${body}" "${PROJECT}" "${DATASET}" <<'PY'
import json, sys
path, project, dataset = sys.argv[1:]
# Views the demo questions actually read. Staging tables are omitted.
tables = ["v_ci_health", "v_outcomes", "v_verdict_rates", "v_path_reach"]
context = {
    "systemInstruction": (
        "Answer questions about CI health and past e2e-agent investigations. "
        "v_ci_health is daily workflow success and failure counts. "
        "v_outcomes joins an investigation to the GitHub run and the finding "
        "(verdict, failed test, audit_passed, exit_reason). "
        "v_verdict_rates summarizes findings. "
        "v_path_reach counts which source files investigations open. "
        "Quote run ids, paths, and verdicts from the query result."
    ),
    "datasourceReferences": {
        "bq": {
            "tableReferences": [
                {"projectId": project, "datasetId": dataset, "tableId": table}
                for table in tables
            ]
        }
    },
}
body = {
    "displayName": "environment-intelligence",
    "description": (
        "History and trends over the ci_agent_eval warehouse: "
        "CI runs and e2e-agent investigations."
    ),
    "dataAnalyticsAgent": {
        "stagingContext": context,
        "publishedContext": context,
    },
}
with open(path, "w") as out:
    json.dump(body, out)
PY

echo "Creating ${NAME}"
http_code="$(
  curl -sS --max-time "${CURL_MAX_TIME}" -o "${tmp}" -w '%{http_code}' \
    -X POST \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "x-goog-user-project: ${PROJECT}" \
    -H "Content-Type: application/json" \
    --data-binary @"${body}" \
    "${HOST}/${PARENT}/dataAgents:createSync?dataAgentId=${DATA_AGENT_ID}"
)"

python3 - "${tmp}" "${http_code}" "${NAME}" <<'PY'
import json, sys
path, code, expected = sys.argv[1], sys.argv[2], sys.argv[3]
raw = open(path).read()
try:
    data = json.loads(raw) if raw.strip() else {}
except json.JSONDecodeError:
    data = {"raw": raw}
if code == "409":
    raise SystemExit(0)
if code not in {"200", "201"} or "error" in data:
    sys.stderr.write(f"createSync returned HTTP {code}\n")
    json.dump(data, sys.stderr, indent=2)
    sys.stderr.write("\n")
    raise SystemExit(1)
name = data.get("name") or expected
if "dataAgents/" not in name:
    sys.stderr.write(f"createSync returned {name}, expected {expected}\n")
    raise SystemExit(1)
PY

echo "ENV_INTELLIGENCE_AGENT=${NAME}"
