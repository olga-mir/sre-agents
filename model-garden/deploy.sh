#!/usr/bin/env bash
# Deploy google/gemma4@gemma-4-26b-a4b-it to a dedicated Agent Platform endpoint.
#
# This bills continuously while the endpoint is up, including at zero traffic.
# List price for the default machine in us-central1:
#   g4-standard-48 + 1x NVIDIA_RTX_PRO_6000 = $5.1751 / hour (~$124 / day).
# Actual bill can differ with discounts, reservations, or a non-us-central1 region.
#
# Refuses to run unless CONFIRM=yes.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ID="${GOOGLE_CLOUD_PROJECT:?set GOOGLE_CLOUD_PROJECT}"
LOCATION_ID="${LOCATION_ID:-us-central1}"
MODEL_ID="google/gemma4@gemma-4-26b-a4b-it"
MACHINE_TYPE="${MACHINE_TYPE:-g4-standard-48}"
ACCELERATOR_TYPE="${ACCELERATOR_TYPE:-NVIDIA_RTX_PRO_6000}"
ACCELERATOR_COUNT="${ACCELERATOR_COUNT:-1}"
IMAGE_URI="us-docker.pkg.dev/vertex-ai/vertex-vision-model-garden-dockers/pytorch-vllm-serve:gemma4"
DISPLAY_NAME="${ENDPOINT_DISPLAY_NAME:-ci-agent-gemma-4-26b-a4b-it}"

echo "Project:     ${PROJECT_ID}"
echo "Region:      ${LOCATION_ID}"
echo "Model:       ${MODEL_ID}"
echo "Machine:     ${MACHINE_TYPE} (${ACCELERATOR_COUNT}x ${ACCELERATOR_TYPE})"
echo "Image:       ${IMAGE_URI}"
echo "List price:  \$5.1751/hour on-demand in us-central1 (snapshot 2026-07-08) for the default machine."
echo "             About \$124/day if left running. The GPU bills at zero traffic until undeploy."
echo "Large models often take 15-20 minutes to start serving."

if [[ "${CONFIRM:-}" != "yes" ]]; then
  echo "Refusing to deploy. Re-run with CONFIRM=yes after accepting that hourly cost." >&2
  exit 1
fi

SPOT_FLAG=()
if [[ "${SPOT:-}" == "1" ]]; then
  SPOT_FLAG=(--spot)
  echo "Spot VM requested. Preemption drops the replica; this script does not quote a spot price."
fi

gcloud services enable aiplatform.googleapis.com --project="${PROJECT_ID}"

# Image + machine select the catalog row "vLLM 256K context".
# That row already passes --enable-auto-tool-choice, --tool-call-parser=gemma4,
# and --reasoning-parser=gemma4. Do not override --container-args.
gcloud ai model-garden models deploy \
  --project="${PROJECT_ID}" \
  --region="${LOCATION_ID}" \
  --model="${MODEL_ID}" \
  --machine-type="${MACHINE_TYPE}" \
  --accelerator-type="${ACCELERATOR_TYPE}" \
  --accelerator-count="${ACCELERATOR_COUNT}" \
  --container-image-uri="${IMAGE_URI}" \
  --endpoint-display-name="${DISPLAY_NAME}" \
  --use-dedicated-endpoint \
  --accept-eula \
  --container-deployment-timeout-seconds=3600 \
  --asynchronous \
  "${SPOT_FLAG[@]}"

echo
echo "Deployment started. Poll with:"
echo "  gcloud ai operations describe OPERATION_ID --region=${LOCATION_ID} --project=${PROJECT_ID}"
echo "Then list endpoints and copy the id into ${ROOT}/endpoint.env:"
echo "  gcloud ai endpoints list --region=${LOCATION_ID} --project=${PROJECT_ID}"
