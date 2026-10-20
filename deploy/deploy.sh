#!/usr/bin/env bash
# Deploy e2e-agent or sre-coordinator with agents-cli.
#
#   task deploy
#   task deploy AGENT=sre-coordinator
#
# agents-cli writes one deployment_metadata.json and copies the project
# .env into the runtime. This script keeps a metadata file per engine
# under deploy/metadata/ (gitignored) and moves .env out of the tree
# before the CLI runs, so GITHUB_TOKEN is not deployed. The runtime env
# is deploy/e2e-agent.env or deploy/sre-coordinator.env, plus the
# variables checked below.
#
# agents-cli grants a baseline Agent Identity role set on first create.
# Secret access, BigQuery, the completions bucket, and the coordinator's
# data-agent roles still come from the grant scripts.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

AGENT="${AGENT:-e2e}"
PROJECT="${GOOGLE_CLOUD_PROJECT:?set GOOGLE_CLOUD_PROJECT}"
REGION="${AGENT_LOCATION:-us-central1}"
META_DIR="${ROOT}/deploy/metadata"

case "${AGENT}" in
  e2e|e2e-agent)
    NAME=e2e-agent
    ENV_FILE=deploy/e2e-agent.env
    : "${GITHUB_APP_ID:?set GITHUB_APP_ID}"
    : "${GITHUB_APP_INSTALLATION_ID:?set GITHUB_APP_INSTALLATION_ID}"
    ;;
  sre|sre-coordinator|sre_coordinator)
    NAME=sre-coordinator
    ENV_FILE=deploy/sre-coordinator.env
    : "${ENV_INTELLIGENCE_AGENT:?set ENV_INTELLIGENCE_AGENT}"
    ;;
  *)
    echo "AGENT must be e2e or sre-coordinator" >&2
    exit 1
    ;;
esac

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

if [[ "${NAME}" == "e2e-agent" ]]; then
  append GITHUB_APP_ID "${GITHUB_APP_ID}"
  append GITHUB_APP_INSTALLATION_ID "${GITHUB_APP_INSTALLATION_ID}"
else
  append ENV_INTELLIGENCE_AGENT "${ENV_INTELLIGENCE_AGENT}"
  if [[ -n "${E2E_AGENT_ENGINE:-}" ]]; then
    append E2E_AGENT_ENGINE "${E2E_AGENT_ENGINE}"
  fi
fi

dotenv_hold=""
METADATA_BAK=""

cleanup() {
  local status=$?
  if [[ -f deployment_metadata.json ]]; then
    mkdir -p "${META_DIR}"
    cp deployment_metadata.json "${META_DIR}/${NAME}.json"
    rm -f deployment_metadata.json
  fi
  if [[ -n "${METADATA_BAK}" && -f "${METADATA_BAK}" ]]; then
    mv "${METADATA_BAK}" "${ROOT}/deployment_metadata.json"
  fi
  if [[ -n "${dotenv_hold}" && -f "${dotenv_hold}" ]]; then
    mv "${dotenv_hold}" "${ROOT}/.env"
  fi
  exit "${status}"
}
trap cleanup EXIT

# agents-cli packages whatever .gitignore does not exclude and copies a
# project-root .env into the runtime. Hold the file outside the repo.
if [[ -f .env ]]; then
  dotenv_hold="$(mktemp)"
  mv .env "${dotenv_hold}"
fi

if [[ -f deployment_metadata.json ]]; then
  METADATA_BAK="$(mktemp)"
  mv deployment_metadata.json "${METADATA_BAK}"
fi
if [[ -f "${META_DIR}/${NAME}.json" ]]; then
  cp "${META_DIR}/${NAME}.json" deployment_metadata.json
fi

agents-cli deploy \
  --deployment-target agent_runtime \
  --project "${PROJECT}" \
  --region "${REGION}" \
  --service-name "${NAME}" \
  --agent-identity \
  --no-confirm-project \
  --update-env-vars "${ENV_VARS}"

if [[ "${NAME}" == "e2e-agent" ]]; then
  echo "If this is the first deploy of e2e-agent, run deploy/grant_agent_identity_iam.sh"
else
  echo "If this is the first deploy of sre-coordinator, run deploy/grant_coordinator_iam.sh"
fi
