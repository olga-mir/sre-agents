#!/usr/bin/env bash
# Dataset access entries for the grant scripts. Source this; do not execute it.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  echo "source deploy/bq_dataset_iam.sh from a grant script" >&2
  exit 1
fi
#
# grant_dataset_role MEMBER ROLE PRIMITIVE
# bq add-iam-policy-binding calls an allowlisted dataset IAM API and fails
# on a normal project with "This feature requires allowlisting". BigQuery
# stores dataEditor as WRITER and dataViewer as READER.

grant_dataset_role() {
  local member="$1"
  local role="$2"
  local primitive="$3"
  local project="${PROJECT:?set PROJECT}"
  local location="${LOCATION:?set LOCATION}"
  local dataset="${DATASET:?set DATASET}"
  local dataset_json status

  dataset_json="$(mktemp)"
  set +e
  bq show --format=prettyjson --location="${location}" "${project}:${dataset}" \
    | MEMBER="${member}" ROLE="${role}" PRIMITIVE="${primitive}" python3 -c '
import json, os, sys
ds = json.load(sys.stdin)
member = os.environ["MEMBER"]
role = os.environ["ROLE"]
equivalent = {role, os.environ["PRIMITIVE"]}
access = ds.setdefault("access", [])
if any(e.get("iamMember") == member and e.get("role") in equivalent for e in access):
    sys.exit(2)
access.append({"role": role, "iamMember": member})
json.dump(ds, sys.stdout, indent=2)
sys.stdout.write("\n")
' > "${dataset_json}"
  status=$?
  set -e
  if [[ "${status}" -eq 0 ]]; then
    bq update --source "${dataset_json}" "${project}:${dataset}"
    echo "Granted ${member} ${role} on ${project}:${dataset}"
  elif [[ "${status}" -eq 2 ]]; then
    echo "Dataset binding already present on ${project}:${dataset} (${role})"
  else
    rm -f "${dataset_json}"
    return "${status}"
  fi
  rm -f "${dataset_json}"
}
