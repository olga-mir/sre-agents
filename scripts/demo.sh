#!/usr/bin/env bash
# Demo cue card. Opens the pages and prints what to paste at each step.
# It does not invoke the agent, query BigQuery, or change anything.
#
#   scripts/demo.sh              # asks for the Actions run id at step 2
#   scripts/demo.sh 33259998849  # run id already chosen
#
# GEMINI_ENTERPRISE_URL opens the Gemini Enterprise agents page.
set -euo pipefail

PROJECT="${GOOGLE_CLOUD_PROJECT:?set GOOGLE_CLOUD_PROJECT}"
REPO="${REPO:-agent-substrate/substrate}"
RUN="${1:-${RUN:-}}"
GE_URL="${GEMINI_ENTERPRISE_URL:-}"

ACTIONS_URL="https://github.com/${REPO}/actions"
BQ_URL="https://console.cloud.google.com/bigquery/agents_hub?project=${PROJECT}"
REGISTRY_URL="https://console.cloud.google.com/agent-platform/agent-registry?project=${PROJECT}"

open_url() {
  printf '  %s\n' "$1"
  if command -v open >/dev/null 2>&1; then
    open "$1"
  fi
}

copy() {
  printf '  %s\n' "$1"
  if command -v pbcopy >/dev/null 2>&1; then
    printf '%s' "$1" | pbcopy
    echo "  (copied to the clipboard)"
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

open_ge() {
  if [[ -n "${GE_URL}" ]]; then
    open_url "${GE_URL}"
  else
    echo "Set GEMINI_ENTERPRISE_URL to open Gemini Enterprise."
  fi
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

INVESTIGATE_Q="can you please investigate this failure: ${RUN} on ${REPO} github repo?"
INVESTIGATE_JSON="{\"trigger_source\": \"TEST_OPERATOR\", \"repo\": \"${REPO}\", \"run_id\": \"${RUN}\"}"
ANALYTICS_Q="Which source files do the most investigations open?"

step "2/5  Gemini Enterprise — sre_coordinator (routing)"
echo "Start a session with sre_coordinator. Paste the investigate question, then send:"
copy "${INVESTIGATE_Q}"
open_ge
pause

echo "Still in that session, ask the analytics question — same coordinator, different route:"
copy "${ANALYTICS_Q}"
pause

step "3/5  Gemini Enterprise — e2e-agent direct (HITL)"
echo "Start a NEW session, this time picking e2e-agent directly (not sre_coordinator)."
echo "HITL only surfaces on this direct path — routing through sre_coordinator drops it."
echo "Direct invocation has no LLM in front to parse natural language into the"
echo "graph's input_schema, so it needs the exact TriggerEvent JSON:"
copy "${INVESTIGATE_JSON}"
open_ge
pause

step "4/5  BigQuery agent"
echo "Ask it something about the warehouse, for example:"
copy "${ANALYTICS_Q}"
open_url "${BQ_URL}"
pause

step "5/5  Agent Registry"
echo "Registered agents for this project. e2e-agent is the GEAP runtime."
open_url "${REGISTRY_URL}"
echo
echo "Done."
