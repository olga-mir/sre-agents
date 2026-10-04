#!/usr/bin/env bash
# Grant the e2e-agent Agent Identity the roles its runtime calls need,
# grant the Vertex evaluation service agent access to the trace view that
# online monitors query, and grant metrics write so OTLP
# MetricsService.Export is not rejected.
# Run after `task deploy` once the engine reports an agent principal.
# The deploy script prints that principal as "IAM member:".
#
# Dataset write access is a dataset access entry (iamMember). bq
# add-iam-policy-binding calls an allowlisted dataset IAM API and fails
# on a normal project with "This feature requires allowlisting".
#
# Usage:
#   ./deploy/grant_agent_identity_iam.sh
#   MEMBER='principal://agents.global.org-ORG.system.id.goog/resources/aiplatform/projects/NUM/locations/us-central1/reasoningEngines/ID' \
#     ./deploy/grant_agent_identity_iam.sh
set -euo pipefail

PROJECT="${PROJECT:-${GOOGLE_CLOUD_PROJECT:?set PROJECT or GOOGLE_CLOUD_PROJECT}}"
LOCATION="${LOCATION:-us-central1}"
DISPLAY_NAME="${DISPLAY_NAME:-e2e-agent}"
SECRET="${SECRET:-e2e-agent-github-app-private-key}"
DATASET="${DATASET:-ci_agent_eval}"
COMPLETIONS_BUCKET="${COMPLETIONS_BUCKET:-${PROJECT}-e2e-agent-completions}"

if [[ -z "${MEMBER:-}" ]]; then
  MEMBER="$(
    curl -fsS \
      -H "Authorization: Bearer $(gcloud auth print-access-token)" \
      -H "x-goog-user-project: ${PROJECT}" \
      "https://${LOCATION}-aiplatform.googleapis.com/v1beta1/projects/${PROJECT}/locations/${LOCATION}/reasoningEngines?pageSize=100" \
    | PROJECT="${PROJECT}" DISPLAY_NAME="${DISPLAY_NAME}" python3 -c '
import json, os, sys
data = json.load(sys.stdin)
name = os.environ["DISPLAY_NAME"]
matches = [e for e in data.get("reasoningEngines", []) if e.get("displayName") == name]
if len(matches) != 1:
    sys.exit(f"expected one reasoning engine named {name!r}, found {len(matches)}")
identity = matches[0].get("spec", {}).get("effectiveIdentity") or ""
if not identity:
    sys.exit(f"{name} has no effectiveIdentity yet")
print(identity)
'
  )"
fi

case "${MEMBER}" in
  principal://agents.*) ;;
  principal://*)
    echo "MEMBER is not an Agent Identity principal: ${MEMBER}" >&2
    exit 1
    ;;
  agents.*)
    MEMBER="principal://${MEMBER}"
    ;;
  *)
    echo "e2e-agent is still using ${MEMBER}." >&2
    echo "Deploy with Agent Identity first, then rerun this script." >&2
    exit 1
    ;;
esac

echo "Granting ${MEMBER}"

# Online monitors query _Trace.Spans._AllSpans as the Vertex eval service
# agent. Without observability.views.access that query returns
# Permission denied on .../views/_AllSpans.
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT}" --format='value(projectNumber)')"
gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-vertex-eval.iam.gserviceaccount.com" \
  --role="roles/observability.viewAccessor" \
  --condition=None

gcloud storage buckets add-iam-policy-binding "gs://${COMPLETIONS_BUCKET}" \
  --member="${MEMBER}" \
  --role="roles/storage.objectUser"

gcloud secrets add-iam-policy-binding "${SECRET}" \
  --project="${PROJECT}" \
  --member="${MEMBER}" \
  --role="roles/secretmanager.secretAccessor" \
  --condition=None

gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="${MEMBER}" \
  --role="roles/bigquery.jobUser" \
  --condition=None

# ADK exports OTLP metrics to telemetry.googleapis.com/v1/metrics. Traces
# and logs already succeed via the reasoning-engine service agent's
# tracesWriter and logWriter roles; metrics need monitoring.timeSeries.create,
# which is only in roles/telemetry.metricsWriter. Both identities that can
# make MetricsService.Export get it.
gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="${MEMBER}" \
  --role="roles/telemetry.metricsWriter" \
  --condition=None
gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-aiplatform-re.iam.gserviceaccount.com" \
  --role="roles/telemetry.metricsWriter" \
  --condition=None

dataset_json="$(mktemp)"
trap 'rm -f "${dataset_json}"' EXIT
set +e
bq show --format=prettyjson --location="${LOCATION}" "${PROJECT}:${DATASET}" \
  | MEMBER="${MEMBER}" python3 -c '
import json, os, sys
ds = json.load(sys.stdin)
member = os.environ["MEMBER"]
role = "roles/bigquery.dataEditor"
# BigQuery stores this grant as the dataset primitive WRITER.
equivalent = {role, "WRITER"}
access = ds.setdefault("access", [])
if any(e.get("iamMember") == member and e.get("role") in equivalent for e in access):
    sys.exit(2)
access.append({"role": role, "iamMember": member})
json.dump(ds, sys.stdout, indent=2)
sys.stdout.write("\n")
' > "${dataset_json}"
dataset_status=$?
set -e
if [[ "${dataset_status}" -eq 0 ]]; then
  bq update --source "${dataset_json}" "${PROJECT}:${DATASET}"
  echo "Granted ${MEMBER} roles/bigquery.dataEditor on ${PROJECT}:${DATASET}"
elif [[ "${dataset_status}" -eq 2 ]]; then
  echo "Dataset binding already present on ${PROJECT}:${DATASET}"
else
  exit "${dataset_status}"
fi

echo "Done."
