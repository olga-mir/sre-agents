"""Create the environment-intelligence data agent.

deploy/data_agent.sh enables APIs, checks the dataset, and runs this.
The create body is deploy/data_agent.json. DATASET_ID in that file is
replaced with the dataset. projectId is not in the file; the request sets
it from the project argument because the API requires it. publishedContext
is copied from stagingContext when the file omits it, so the instruction is
written once and the agent is created already published.

CRUD for location `us` is on the regional endpoint. The global host
geminidataanalytics.googleapis.com returns 403 "Read access to project
was denied" for a project owner.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

CONFIG = Path(__file__).with_name("data_agent.json")


class DataAgentError(Exception):
    pass


def stamp(node, dataset: str):
    """Replace sentinel values. A longer string that mentions one is left as it is."""
    if isinstance(node, dict):
        return {key: stamp(value, dataset) for key, value in node.items()}
    if isinstance(node, list):
        return [stamp(value, dataset) for value in node]
    if node == "DATASET_ID":
        return dataset
    return node


def _table_refs(context: dict) -> list:
    refs = context.get("datasourceReferences", {}).get("bq", {}).get("tableReferences")
    if not isinstance(refs, list):
        raise DataAgentError("data_agent.json has no tableReferences")
    return refs


def request_body(doc: dict, project: str, dataset: str) -> dict:
    body = stamp(doc, dataset)
    agent = body.get("dataAnalyticsAgent")
    if not isinstance(agent, dict) or "stagingContext" not in agent:
        raise DataAgentError("data_agent.json has no dataAnalyticsAgent.stagingContext")
    if "publishedContext" not in agent:
        agent["publishedContext"] = stamp(agent["stagingContext"], dataset)
    for context_name in ("stagingContext", "publishedContext"):
        context = agent.get(context_name)
        if not isinstance(context, dict):
            continue
        for ref in _table_refs(context):
            if not isinstance(ref, dict):
                raise DataAgentError("table reference is not an object")
            ref["projectId"] = project
    return body


def load_config() -> dict:
    return json.loads(CONFIG.read_text())


def classify_get(status: int, payload: dict, name: str) -> str:
    """'exists' or 'missing'. A soft-deleted agent is still HTTP 200."""
    if status == 404:
        return "missing"
    if status != 200:
        raise DataAgentError(f"GET {name} returned HTTP {status}\n{_dump(payload)}")
    if payload.get("deleteTime"):
        purge = payload.get("purgeTime") or "its purge time"
        raise DataAgentError(
            f"Data agent {name} is soft-deleted until {purge}.\n"
            "That id cannot be reused before then. Rerun with a new DATA_AGENT_ID."
        )
    return "exists"


def classify_create(status: int, payload: dict, expected: str) -> None:
    if status == 409:
        return
    if status not in (200, 201) or "error" in payload:
        raise DataAgentError(f"createSync returned HTTP {status}\n{_dump(payload)}")
    name = payload.get("name") or expected
    if "dataAgents/" not in name:
        raise DataAgentError(f"createSync returned {name}, expected {expected}")


def resource_name(project: str, location: str, agent_id: str) -> str:
    return (
        f"projects/{quote(project, safe='')}"
        f"/locations/{quote(location, safe='')}"
        f"/dataAgents/{quote(agent_id, safe='')}"
    )


def endpoint(location: str) -> str:
    return f"https://geminidataanalytics.{quote(location, safe='')}.rep.googleapis.com/v1beta"


def _dump(payload: dict) -> str:
    return json.dumps(payload, indent=2)


def _call(method: str, url: str, token: str, project: str, timeout: float, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "x-goog-user-project": project,
            "Content-Type": "application/json",
        },
    )
    try:
        # urlopen blocks forever when timeout is omitted: the global default
        # is None unless socket.setdefaulttimeout was called. timeout is
        # CURL_MAX_TIME (default 120s) for this one call.
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            parsed = json.loads(raw) if raw.strip() else {}
            return resp.status, parsed
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            parsed = {"raw": raw.decode("utf-8", "replace")}
        return exc.code, parsed
    except urllib.error.URLError as exc:
        raise DataAgentError(f"{method} {url} failed: {exc.reason}") from exc


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise DataAgentError(f"set {name}")
    return value


def run() -> None:
    project = os.environ.get("PROJECT", "").strip() or os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    if not project:
        raise DataAgentError("set PROJECT or GOOGLE_CLOUD_PROJECT")
    dataset = os.environ.get("DATASET", "").strip() or os.environ.get("BQ_DATASET", "").strip() or "ci_agent_eval"
    location = os.environ.get("DATA_AGENT_LOCATION", "").strip() or "us"
    agent_id = os.environ.get("DATA_AGENT_ID", "").strip() or "environment-intelligence"
    timeout = float(os.environ.get("CURL_MAX_TIME", "").strip() or "120")
    token = _required("DATA_AGENT_TOKEN")

    name = resource_name(project, location, agent_id)
    host = endpoint(location)
    parent = name.rsplit("/dataAgents/", 1)[0]
    status, payload = _call("GET", f"{host}/{name}", token, project, timeout)
    if classify_get(status, payload, name) == "exists":
        print(f"Data agent already exists: {name}")
        print(f"ENV_INTELLIGENCE_AGENT={name}")
        return

    print(f"Creating {name}")
    body = request_body(load_config(), project, dataset)
    status, payload = _call(
        "POST",
        f"{host}/{parent}/dataAgents:createSync?dataAgentId={quote(agent_id, safe='')}",
        token,
        project,
        timeout,
        body,
    )
    classify_create(status, payload, name)
    print(f"ENV_INTELLIGENCE_AGENT={name}")


def main() -> int:
    try:
        run()
    except DataAgentError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
