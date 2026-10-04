#!/usr/bin/env bash
# Chat and tool-call probes against a dedicated Model Garden endpoint.
# Reads model-garden/endpoint.env (see endpoint.env.example).

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${ROOT}/endpoint.env"
if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}. Copy endpoint.env.example and set ENDPOINT_ID." >&2
  exit 1
fi
# shellcheck disable=SC1090
source "${ENV_FILE}"

: "${GOOGLE_CLOUD_PROJECT:?}"
: "${LOCATION_ID:?}"
: "${ENDPOINT_ID:?}"
export SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-google/gemma-4-26B-A4B-it}"
export PROMPT="${1:-Reply with the single word pong.}"

echo "Fetching dedicated endpoint DNS..."
ENDPOINT_DNS="$(gcloud ai endpoints describe "${ENDPOINT_ID}" \
  --project="${GOOGLE_CLOUD_PROJECT}" \
  --region="${LOCATION_ID}" \
  --format="value(dedicatedEndpointDns)")"
if [[ -z "${ENDPOINT_DNS}" ]]; then
  echo "No dedicatedEndpointDns. The deploy must use --use-dedicated-endpoint." >&2
  exit 1
fi

BASE="https://${ENDPOINT_DNS}/v1beta1/projects/${GOOGLE_CLOUD_PROJECT}/locations/${LOCATION_ID}/endpoints/${ENDPOINT_ID}"
AUTH=(-H "Authorization: Bearer $(gcloud auth print-access-token)" -H "Content-Type: application/json")

echo "Chat completion against ${BASE}/chat/completions"
curl -sS -X POST "${AUTH[@]}" \
  "${BASE}/chat/completions" \
  -d "$(python3 -c '
import json, os
print(json.dumps({
  "model": os.environ["SERVED_MODEL_NAME"],
  "messages": [{"role": "user", "content": os.environ["PROMPT"]}],
  "max_tokens": 64,
  "chat_template_kwargs": {"enable_thinking": False},
}))
')"

echo
echo "Tool-call probe (expect a tool_calls entry, not prose)"
curl -sS -X POST "${AUTH[@]}" \
  "${BASE}/chat/completions" \
  -d "$(python3 -c '
import json, os
print(json.dumps({
  "model": os.environ["SERVED_MODEL_NAME"],
  "messages": [{"role": "user", "content": "What is 2+2? Use the add tool."}],
  "max_tokens": 128,
  "chat_template_kwargs": {"enable_thinking": False},
  "tools": [{
    "type": "function",
    "function": {
      "name": "add",
      "description": "Add two integers.",
      "parameters": {
        "type": "object",
        "properties": {
          "a": {"type": "integer"},
          "b": {"type": "integer"},
        },
        "required": ["a", "b"],
      },
    },
  }],
}))
')"
echo
