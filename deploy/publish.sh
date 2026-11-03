#!/usr/bin/env bash
# Register a deployed agent with a Gemini Enterprise app, or list the apps.
#
#   deploy/publish.sh <agent>
#
# Environment: GOOGLE_CLOUD_PROJECT, AGENT_LOCATION (default us-central1),
# GEMINI_ENTERPRISE_APP_ID (app id or full resource name; unset lists the
# apps), ENGINE (optional engine resource name; default: look up the runtime
# whose display name is the agent's NAME).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${HERE}/lib.sh"

resolve_agent "${1:-${AGENT:-e2e}}"
PROJECT="${GOOGLE_CLOUD_PROJECT:?set GOOGLE_CLOUD_PROJECT}"
REGION="${AGENT_LOCATION:-us-central1}"
APP_ID="${GEMINI_ENTERPRISE_APP_ID:-}"
ENGINE="${ENGINE:-}"
PY="${ROOT}/.venv/bin/python"

if ! command -v agents-cli >/dev/null 2>&1; then
  echo "agents-cli not on PATH. Install: uv tool install google-agents-cli" >&2
  exit 1
fi

if [[ -z "${APP_ID}" ]]; then
  agents-cli publish gemini-enterprise --list --project-id "${PROJECT}"
  exit 0
fi

case "${APP_ID}" in
  projects/*) APP="${APP_ID}" ;;
  *) APP="projects/${PROJECT}/locations/global/collections/default_collection/engines/${APP_ID}" ;;
esac

if [[ -z "${ENGINE}" ]]; then
  ENGINE="$(GOOGLE_CLOUD_PROJECT="${PROJECT}" NAME="${NAME}" AGENT_LOCATION="${REGION}" "${PY}" -c "
import agentplatform, os, sys
name = os.environ['NAME']
client = agentplatform.Client(project=os.environ['GOOGLE_CLOUD_PROJECT'], location=os.environ['AGENT_LOCATION'])
for r in client.runtimes.list():
    if getattr(r.api_resource, 'display_name', None) == name:
        print(r.api_resource.name)
        sys.exit(0)
sys.exit(f'no Agent Runtime named {name}; pass ENGINE=projects/.../reasoningEngines/...')
")"
fi

echo "Publishing ${ENGINE} to ${APP} as ADK (${NAME})..."
# agents-cli also reads GEMINI_ENTERPRISE_APP_ID. Pass the full name
# so a short id in .env is not what the CLI sees.
GEMINI_ENTERPRISE_APP_ID="${APP}" agents-cli publish gemini-enterprise \
  --registration-type adk \
  --deployment-target agent_runtime \
  --agent-runtime-id "${ENGINE}" \
  --gemini-enterprise-app-id "${APP}" \
  --display-name "${NAME}" \
  --description "${PUBLISH_DESCRIPTION}" \
  --tool-description "${PUBLISH_TOOL_DESCRIPTION}" \
  --project-id "${PROJECT}"
