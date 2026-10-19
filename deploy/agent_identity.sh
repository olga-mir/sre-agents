#!/usr/bin/env bash
# Lookup for the grant scripts. Source this; do not execute it.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  echo "source deploy/agent_identity.sh from a grant script" >&2
  exit 1
fi
#
# agent_identity DISPLAY_NAME
# Prints a principal://agents... member. Uses MEMBER when it is already set.
# curl with no --max-time waits forever.

agent_identity() {
  local display_name="$1"
  local member="${MEMBER:-}"
  local project="${PROJECT:?set PROJECT}"
  local location="${LOCATION:?set LOCATION}"

  if [[ -z "${member}" ]]; then
    # curl default: no timeout.
    member="$(
      curl --max-time 30 -fsS \
        -H "Authorization: Bearer $(gcloud auth print-access-token)" \
        -H "x-goog-user-project: ${project}" \
        "https://${location}-aiplatform.googleapis.com/v1beta1/projects/${project}/locations/${location}/reasoningEngines?pageSize=100" \
      | DISPLAY_NAME="${display_name}" python3 -c '
import json, os, sys
data = json.load(sys.stdin)
name = os.environ["DISPLAY_NAME"]
matches = [e for e in data.get("reasoningEngines", []) if e.get("displayName") == name]
if len(matches) != 1:
    sys.exit(f"expected one reasoning engine named {name!r}, found {len(matches)}")
identity = matches[0].get("spec", {}).get("effectiveIdentity") or ""
if not identity:
    sys.exit(f"{name} has no effectiveIdentity yet")
print(identity)
'
    )"
  fi

  case "${member}" in
    principal://agents.*) ;;
    principal://*)
      echo "MEMBER is not an Agent Identity principal: ${member}" >&2
      return 1
      ;;
    agents.*)
      member="principal://${member}"
      ;;
    *)
      echo "${display_name} is still using ${member}." >&2
      echo "Deploy with Agent Identity first, then rerun this script." >&2
      return 1
      ;;
  esac
  printf '%s\n' "${member}"
}
