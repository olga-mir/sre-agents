#!/usr/bin/env bash
# Pull Agent Runtime session events for e2e-agent and save the newest sessions.
#
# REST (v1beta1 Sessions API, not :streamQuery):
#   GET .../reasoningEngines/ENGINE/sessions
#   GET .../reasoningEngines/ENGINE/sessions/SID
#   GET .../reasoningEngines/ENGINE/sessions/SID/events
#
# Pulls sessions for the engine named e2e-agent.
# Override with env vars: GOOGLE_CLOUD_PROJECT, AGENT_LOCATION, ENGINE, LIMIT, OUT.
set -euo pipefail

PROJECT="${GOOGLE_CLOUD_PROJECT:?set GOOGLE_CLOUD_PROJECT}"
LOCATION="${AGENT_LOCATION:-us-central1}"
DISPLAY_NAME="${AGENT_DISPLAY_NAME:-e2e-agent}"
ENGINE="${ENGINE:-${ENGINE_ID:-}}"
LIMIT="${LIMIT:-2}"
OUT="${OUT:-runs/session-events}"

TOKEN="$(gcloud auth print-access-token)"
HOST="https://${LOCATION}-aiplatform.googleapis.com"

urlencode() {
  python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"
}

if [[ -z "${ENGINE}" ]]; then
  echo "GET ${HOST}/v1/projects/${PROJECT}/locations/${LOCATION}/reasoningEngines"
  ENGINE="$(
    curl -fsS -H "Authorization: Bearer ${TOKEN}" \
      "${HOST}/v1/projects/${PROJECT}/locations/${LOCATION}/reasoningEngines" \
      | DISPLAY_NAME="${DISPLAY_NAME}" python3 -c '
import json, os, sys
want = os.environ["DISPLAY_NAME"]
data = json.load(sys.stdin)
if "error" in data:
    raise SystemExit(json.dumps(data["error"]))
for engine in data.get("reasoningEngines") or []:
    if engine.get("displayName") == want:
        print(engine["name"].rstrip("/").split("/")[-1])
        raise SystemExit
raise SystemExit(f"no reasoning engine named {want!r}")
'
  )"
fi
ENGINE="${ENGINE##*/}"

PARENT="projects/${PROJECT}/locations/${LOCATION}/reasoningEngines/${ENGINE}"
BASE="${HOST}/v1beta1/${PARENT}"
mkdir -p "${OUT}"

tmpdir="$(mktemp -d)"
trap 'rm -rf "${tmpdir}"' EXIT

page_token=""
: > "${tmpdir}/sessions.ndjson"
while true; do
  url="${BASE}/sessions?pageSize=100"
  if [[ -n "${page_token}" ]]; then
    url="${url}&pageToken=$(urlencode "${page_token}")"
  fi
  echo "GET ${url%%\?*}"
  curl -fsS -H "Authorization: Bearer ${TOKEN}" "${url}" -o "${tmpdir}/sessions-page.json"
  page_token="$(
    python3 - "${tmpdir}/sessions-page.json" "${tmpdir}/sessions.ndjson" <<'PY'
import json, sys
page_path, ndjson_path = sys.argv[1], sys.argv[2]
data = json.load(open(page_path))
if "error" in data:
    raise SystemExit(json.dumps(data["error"]))
with open(ndjson_path, "a") as out:
    for session in data.get("sessions") or []:
        out.write(json.dumps(session) + "\n")
print(data.get("nextPageToken") or "")
PY
  )"
  [[ -n "${page_token}" ]] || break
done

mapfile -t SESSION_IDS < <(
  LIMIT="${LIMIT}" python3 - "${tmpdir}/sessions.ndjson" "${OUT}/sessions-index.json" <<'PY'
import json, os, sys
ndjson_path, index_path = sys.argv[1], sys.argv[2]
limit = int(os.environ["LIMIT"])
sessions = [json.loads(line) for line in open(ndjson_path) if line.strip()]
sessions.sort(key=lambda s: s.get("updateTime") or "", reverse=True)
index = []
for session in sessions:
    name = session.get("name") or ""
    index.append({
        "session_id": name.rstrip("/").split("/")[-1],
        "user_id": session.get("userId"),
        "update_time": session.get("updateTime"),
        "create_time": session.get("createTime"),
    })
with open(index_path, "w") as out:
    json.dump({"engine": sessions[0]["name"].rsplit("/sessions/", 1)[0] if sessions else "",
               "sessions": index}, out, indent=2)
    out.write("\n")
for row in index[:limit]:
    print(row["session_id"])
PY
)

if [[ ${#SESSION_IDS[@]} -eq 0 ]]; then
  echo "no sessions on ${PARENT}" >&2
  exit 1
fi

printf '%s\n' "${SESSION_IDS[@]}" > "${OUT}/latest.txt"
echo "saving ${#SESSION_IDS[@]} newest session(s) under ${OUT}/"

for sid in "${SESSION_IDS[@]}"; do
  dest="${OUT}/${sid}"
  mkdir -p "${dest}"
  echo "GET ${BASE}/sessions/${sid}"
  curl -fsS -H "Authorization: Bearer ${TOKEN}" \
    "${BASE}/sessions/${sid}" \
    -o "${dest}/session-resource.json"

  : > "${tmpdir}/events.ndjson"
  page_token=""
  while true; do
    url="${BASE}/sessions/${sid}/events?pageSize=100"
    if [[ -n "${page_token}" ]]; then
      url="${url}&pageToken=$(urlencode "${page_token}")"
    fi
    echo "GET ${url%%\?*}"
    curl -fsS -H "Authorization: Bearer ${TOKEN}" "${url}" -o "${tmpdir}/events-page.json"
    page_token="$(
      python3 - "${tmpdir}/events-page.json" "${tmpdir}/events.ndjson" <<'PY'
import json, sys
page_path, ndjson_path = sys.argv[1], sys.argv[2]
data = json.load(open(page_path))
if "error" in data:
    raise SystemExit(json.dumps(data["error"]))
events = data.get("sessionEvents")
if events is None:
    events = data.get("events") or []
with open(ndjson_path, "a") as out:
    for event in events:
        out.write(json.dumps(event) + "\n")
print(data.get("nextPageToken") or "")
PY
    )"
    [[ -n "${page_token}" ]] || break
  done

  python3 - "${tmpdir}/events.ndjson" "${dest}/events.json" <<'PY'
import json, sys
src, dest = sys.argv[1], sys.argv[2]
events = [json.loads(line) for line in open(src) if line.strip()]
with open(dest, "w") as out:
    json.dump({"sessionEvents": events}, out, indent=2)
    out.write("\n")
print(f"  {dest}  events={len(events)}")
PY
done

echo "index: ${OUT}/sessions-index.json"
