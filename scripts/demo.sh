#!/usr/bin/env bash
# Demo cue card. Opens the pages and prints the playground payload.
# It does not invoke the agent, query BigQuery, or change anything.
#
#   scripts/demo.sh              # asks for the Actions run id at step 2
#   scripts/demo.sh 33259998849  # run id already chosen
#
# ENGINE is the e2e-agent reasoning-engine id. GEMINI_ENTERPRISE_URL opens
# that page on the last step when it is set.
set -euo pipefail

PROJECT="${GOOGLE_CLOUD_PROJECT:?set GOOGLE_CLOUD_PROJECT}"
REGION="${AGENT_LOCATION:-us-central1}"
ENGINE="${ENGINE:?set ENGINE to the e2e-agent reasoning engine id}"
REPO="${REPO:-agent-substrate/substrate}"
RUN="${1:-${RUN:-}}"

ACTIONS_URL="https://github.com/${REPO}/actions"
PLAYGROUND_URL="https://console.cloud.google.com/agent-platform/runtimes/locations/${REGION}/agent-engines/${ENGINE}/playground?project=${PROJECT}"
BQ_URL="https://console.cloud.google.com/bigquery/agents_hub?project=${PROJECT}"
REGISTRY_URL="https://console.cloud.google.com/agent-platform/agent-registry?project=${PROJECT}"
GE_URL="${GEMINI_ENTERPRISE_URL:-}"

open_url() {
  printf '  %s\n' "$1"
  if command -v open >/dev/null 2>&1; then
    open "$1"
  fi
}

pause() {
  echo
  read -r -p "Press Enter when you are back. " _
  echo
}

step() {
  echo
  echo "────────────────────────────────────────"
  echo "  $1"
  echo "────────────────────────────────────────"
}

step "1/5  Substrate Actions"
echo "Pick a failed run. You will paste its run id at the next step."
open_url "${ACTIONS_URL}"
pause

if [[ -z "${RUN}" ]]; then
  read -r -p "Actions run id: " RUN
fi
if [[ ! "${RUN}" =~ ^[0-9]+$ ]]; then
  echo "Run id must be digits (got: ${RUN})" >&2
  exit 1
fi

PAYLOAD="$(python3 -c 'import json,sys; print(json.dumps({"trigger_source":"TEST_OPERATOR","repo":sys.argv[1],"run_id":sys.argv[2]}))' "${REPO}" "${RUN}")"

step "2/5  GEAP playground — paste this, then send"
echo "${PAYLOAD}"
if command -v pbcopy >/dev/null 2>&1; then
  printf '%s' "${PAYLOAD}" | pbcopy
  echo "(copied to the clipboard)"
fi
open_url "${PLAYGROUND_URL}"
echo
echo "Leave that session running. Next step is the BigQuery agent."
pause

step "3/5  BigQuery agent"
echo "Ask it something about the warehouse while the investigation runs, for example:"
echo "  Which source files do the most investigations open?"
open_url "${BQ_URL}"
pause

step "4/5  Agent Registry"
echo "Registered agents for this project. e2e-agent is the GEAP runtime."
open_url "${REGISTRY_URL}"
pause

step "5/5  Gemini Enterprise — Agents from your org"
echo "Point at the two agents: the BigQuery data agent, and e2e-agent from Agent Runtime."
if [[ -n "${GE_URL}" ]]; then
  open_url "${GE_URL}"
else
  echo "Set GEMINI_ENTERPRISE_URL to open that page."
fi
echo
echo "Done. Playground payload was:"
echo "${PAYLOAD}"
