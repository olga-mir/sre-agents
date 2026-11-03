#!/usr/bin/env bash
# Shared by deploy.sh and publish.sh. Source it; do not run it.
#
#   resolve_agent <name-or-alias>   sets ROOT, NAME, AGENT_DIR and loads
#                                   agents/<NAME>/deploy/deploy.conf

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

resolve_agent() {
  case "${1:-}" in
    e2e|e2e-agent|e2e_agent) NAME=e2e-agent ;;
    sre|sre-coordinator|sre_coordinator|coordinator) NAME=sre-coordinator ;;
    *)
      echo "AGENT must be e2e or sre-coordinator (got '${1:-}')" >&2
      return 1
      ;;
  esac
  AGENT_DIR="${ROOT}/agents/${NAME}"
  # shellcheck source=/dev/null
  source "${AGENT_DIR}/deploy/deploy.conf"
}
