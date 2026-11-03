#!/usr/bin/env bash
# Deploy one agent to Agent Runtime with agents-cli.
#
#   task deploy:e2e
#   task deploy:coordinator
#   deploy/deploy.sh e2e-agent        # what the tasks run
#
# Each agent is its own deployment. It owns, under agents/<agent>/:
#   deploy/deploy.conf   name, required env, Gemini Enterprise description
#   deploy/runtime.env   the pinned production env (AGENTS.md invariant 11)
#   deploy/deployment_metadata.json   the engine this agent updates (gitignored)
#   Dockerfile, pyproject.toml, requirements.txt, agents-cli-manifest.yaml
#
# deploy/stage.sh copies the agent plus libs/agent_serving into a temp dir and
# agents-cli runs there, so one agent's metadata, image inputs and env can
# never leak into the other's. The temp dir never holds .env.
#
# agents-cli grants a baseline Agent Identity role set on first create.
# Secret access, BigQuery, the completions bucket, and the coordinator's
# data-agent roles still come from agents/<agent>/deploy/grant_iam.sh.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${HERE}/lib.sh"

resolve_agent "${1:-${AGENT:-e2e}}"
PROJECT="${GOOGLE_CLOUD_PROJECT:?set GOOGLE_CLOUD_PROJECT}"
REGION="${AGENT_LOCATION:-us-central1}"
ENV_FILE="${AGENT_DIR}/deploy/runtime.env"
METADATA="${AGENT_DIR}/deploy/deployment_metadata.json"
# Before the per-agent layout, metadata lived here. Without it a deploy
# would create a second engine instead of updating the recorded one.
LEGACY_METADATA="${ROOT}/deploy/metadata/${NAME}.json"

for var in "${REQUIRED_ENV[@]}"; do
  : "${!var:?set ${var}}"
done

if ! command -v agents-cli >/dev/null 2>&1; then
  echo "agents-cli not on PATH. Install: uv tool install google-agents-cli" >&2
  exit 1
fi
if [[ ! -f "${ENV_FILE}" ]]; then
  echo "missing ${ENV_FILE}" >&2
  exit 1
fi

ENV_VARS=""

append() {
  local key="$1" val="$2"
  if [[ "${key}" == *","* || "${val}" == *","* ]]; then
    echo "agents-cli --update-env-vars splits on commas: ${key}" >&2
    exit 1
  fi
  if [[ -n "${ENV_VARS}" ]]; then
    ENV_VARS+=","
  fi
  ENV_VARS+="${key}=${val}"
}

load_env_file() {
  local file="$1" line key val bucket
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line%%$'\r'}"
    [[ "${line}" =~ ^[[:space:]]*# ]] && continue
    [[ -z "${line//[[:space:]]/}" ]] && continue
    if [[ "${line}" != *=* ]]; then
      echo "malformed line in ${file}: ${line}" >&2
      exit 1
    fi
    key="${line%%=*}"
    val="${line#*=}"
    key="${key#"${key%%[![:space:]]*}"}"
    key="${key%"${key##*[![:space:]]}"}"
    val="${val#"${val%%[![:space:]]*}"}"
    val="${val%"${val##*[![:space:]]}"}"
    case "${key}" in
      GOOGLE_CLOUD_PROJECT|GITHUB_TOKEN)
        echo "${file} must not set ${key}" >&2
        exit 1
        ;;
      OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH)
        bucket="${COMPLETIONS_BUCKET:-${PROJECT}-e2e-agent-completions}"
        val="gs://${bucket}/completions"
        ;;
      GITHUB_APP_PRIVATE_KEY_SECRET)
        if [[ -n "${GITHUB_APP_PRIVATE_KEY_SECRET:-}" ]]; then
          val="${GITHUB_APP_PRIVATE_KEY_SECRET}"
        fi
        ;;
      AGENT_LOCATION)
        val="${REGION}"
        ;;
    esac
    append "${key}" "${val}"
  done < "${file}"
}

load_env_file "${ENV_FILE}"

for var in "${FORWARD_ENV[@]}"; do
  append "${var}" "${!var}"
done
for var in "${OPTIONAL_FORWARD_ENV[@]}"; do
  if [[ -n "${!var:-}" ]]; then
    append "${var}" "${!var}"
  fi
done

STAGE="$(mktemp -d)"
cleanup() {
  local status=$?
  if [[ -f "${STAGE}/deployment_metadata.json" ]]; then
    cp "${STAGE}/deployment_metadata.json" "${METADATA}"
  fi
  rm -rf "${STAGE}"
  exit "${status}"
}
trap cleanup EXIT

"${HERE}/stage.sh" "${NAME}" "${STAGE}"

if [[ -f "${METADATA}" ]]; then
  cp "${METADATA}" "${STAGE}/deployment_metadata.json"
elif [[ -f "${LEGACY_METADATA}" ]]; then
  echo "using legacy ${LEGACY_METADATA}; it is saved to ${METADATA} after this deploy" >&2
  cp "${LEGACY_METADATA}" "${STAGE}/deployment_metadata.json"
fi

cd "${STAGE}"
# agents-cli deploy waits for the Agent Runtime operation itself. Its own
# deadline is the CLI's; this script adds no second one around it.
agents-cli deploy \
  --deployment-target agent_runtime \
  --project "${PROJECT}" \
  --region "${REGION}" \
  --service-name "${NAME}" \
  --agent-identity \
  --no-confirm-project \
  --update-env-vars "${ENV_VARS}"

echo "If this is the first deploy of ${NAME}, run agents/${NAME}/deploy/grant_iam.sh"
