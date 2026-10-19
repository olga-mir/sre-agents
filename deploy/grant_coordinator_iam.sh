#!/usr/bin/env bash
# Grant the sre-coordinator Agent Identity the roles its runtime calls need.
# Run after the first `task deploy AGENT=sre-coordinator`, once the engine
# reports an agent principal.
#
#   deploy/grant_coordinator_iam.sh
#   MEMBER='principal://agents.global.org-ORG.system.id.goog/resources/aiplatform/projects/NUM/locations/us-central1/reasoningEngines/ID' \
#     deploy/grant_coordinator_iam.sh
#
# roles/aiplatform.user is what stream_query on e2e-agent requires.
# The data agent runs BigQuery as the caller, so this identity also needs
# jobUser and dataViewer on ci_agent_eval. See
# https://docs.cloud.google.com/bigquery/docs/create-data-agents
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=agent_identity.sh
source "${HERE}/agent_identity.sh"
# shellcheck source=bq_dataset_iam.sh
source "${HERE}/bq_dataset_iam.sh"

PROJECT="${PROJECT:-${GOOGLE_CLOUD_PROJECT:?set PROJECT or GOOGLE_CLOUD_PROJECT}}"
LOCATION="${LOCATION:-us-central1}"
DISPLAY_NAME="${DISPLAY_NAME:-sre-coordinator}"
DATASET="${DATASET:-ci_agent_eval}"

MEMBER="$(agent_identity "${DISPLAY_NAME}")"
echo "Granting ${MEMBER}"

for role in \
  roles/aiplatform.user \
  roles/geminidataanalytics.dataAgentUser \
  roles/cloudaicompanion.user \
  roles/bigquery.jobUser
do
  gcloud projects add-iam-policy-binding "${PROJECT}" \
    --member="${MEMBER}" \
    --role="${role}" \
    --condition=None
done

grant_dataset_role "${MEMBER}" "roles/bigquery.dataViewer" "READER"
echo "Done."
