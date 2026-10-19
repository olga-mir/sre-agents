#!/usr/bin/env bash
# Grant the e2e-agent Agent Identity the roles its runtime calls need,
# grant the Vertex evaluation service agent access to the trace view that
# online monitors query, and grant metrics write so OTLP
# MetricsService.Export is not rejected.
# Run after `task deploy` once the engine reports an agent principal.
#
# Dataset write access is a dataset access entry (iamMember). See
# bq_dataset_iam.sh for why this is not `bq add-iam-policy-binding`.
#
# Usage:
#   deploy/grant_agent_identity_iam.sh
#   MEMBER='principal://agents.global.org-ORG.system.id.goog/resources/aiplatform/projects/NUM/locations/us-central1/reasoningEngines/ID' \
#     deploy/grant_agent_identity_iam.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=agent_identity.sh
source "${HERE}/agent_identity.sh"
# shellcheck source=bq_dataset_iam.sh
source "${HERE}/bq_dataset_iam.sh"

PROJECT="${PROJECT:-${GOOGLE_CLOUD_PROJECT:?set PROJECT or GOOGLE_CLOUD_PROJECT}}"
LOCATION="${LOCATION:-us-central1}"
DISPLAY_NAME="${DISPLAY_NAME:-e2e-agent}"
SECRET="${SECRET:-e2e-agent-github-app-private-key}"
DATASET="${DATASET:-ci_agent_eval}"
COMPLETIONS_BUCKET="${COMPLETIONS_BUCKET:-${PROJECT}-e2e-agent-completions}"

MEMBER="$(agent_identity "${DISPLAY_NAME}")"
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

grant_dataset_role "${MEMBER}" "roles/bigquery.dataEditor" "WRITER"
echo "Done."
