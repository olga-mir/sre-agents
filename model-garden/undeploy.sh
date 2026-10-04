#!/usr/bin/env bash
# Undeploy the model and delete the endpoint and the uploaded model resource.
# Stopping traffic is not enough: the GPU bills until the model is undeployed.
#
# Refuses to run unless CONFIRM=delete.

set -euo pipefail

if [[ "${CONFIRM:-}" != "delete" ]]; then
  echo "Refusing to delete. This removes the endpoint and the model resource." >&2
  echo "Re-run with CONFIRM=delete after you mean it." >&2
  exit 1
fi

ROOT="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${ROOT}/endpoint.env"
if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}." >&2
  exit 1
fi
# shellcheck disable=SC1090
source "${ENV_FILE}"

: "${GOOGLE_CLOUD_PROJECT:?}"
: "${LOCATION_ID:?}"
: "${ENDPOINT_ID:?}"

DEPLOYED_MODEL_ID="$(gcloud ai endpoints describe "${ENDPOINT_ID}" \
  --project="${GOOGLE_CLOUD_PROJECT}" \
  --region="${LOCATION_ID}" \
  --format="value(deployedModels[0].id)")"
MODEL_RESOURCE="$(gcloud ai endpoints describe "${ENDPOINT_ID}" \
  --project="${GOOGLE_CLOUD_PROJECT}" \
  --region="${LOCATION_ID}" \
  --format="value(deployedModels[0].model)")"

if [[ -z "${DEPLOYED_MODEL_ID}" ]]; then
  echo "No deployed model on endpoint ${ENDPOINT_ID}. Nothing to undeploy." >&2
  exit 1
fi

echo "Undeploying ${DEPLOYED_MODEL_ID} from endpoint ${ENDPOINT_ID}..."
gcloud ai endpoints undeploy-model "${ENDPOINT_ID}" \
  --project="${GOOGLE_CLOUD_PROJECT}" \
  --region="${LOCATION_ID}" \
  --deployed-model-id="${DEPLOYED_MODEL_ID}" \
  --quiet

echo "Deleting endpoint ${ENDPOINT_ID}..."
gcloud ai endpoints delete "${ENDPOINT_ID}" \
  --project="${GOOGLE_CLOUD_PROJECT}" \
  --region="${LOCATION_ID}" \
  --quiet

if [[ -n "${MODEL_RESOURCE}" ]]; then
  echo "Deleting model ${MODEL_RESOURCE}..."
  gcloud ai models delete "${MODEL_RESOURCE}" \
    --region="${LOCATION_ID}" \
    --quiet
fi

echo "Cleanup finished. GPU charges stop once undeploy completes."
