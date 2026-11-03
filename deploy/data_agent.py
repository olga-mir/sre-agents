"""Create the environment-intelligence data agent and register its A2A card.

deploy/data_agent.sh enables APIs, checks the dataset, and runs this.
The create body is deploy/data_agent.json. DATASET_ID in that file is
replaced with the dataset. projectId is not in the file; the request sets
it from the project argument because the API requires it. publishedContext
is copied from stagingContext when the file omits it, so the instruction is
written once and the agent is created already published.

CRUD for location `us` is on the regional endpoint. The global host
geminidataanalytics.googleapis.com returns 403 "Read access to project
was denied" for a project owner.

Agent Registry has no dataAgents resource. Registration is a Service whose
agentSpec is the published agent's A2A card. Manual registration is rejected
in multi-regions us and eu, so the service is created in
DATA_AGENT_REGISTRY_LOCATION (default global). An existing data agent and an
existing service are left as they are.

Each HTTP call passes timeout (CURL_MAX_TIME, default 120s). urllib.request
urlopen waits forever when timeout is omitted. One registration is at most
two card GETs, one service GET, one services.create, and
REGISTRY_POLL_ATTEMPTS operations.get calls, plus
(REGISTRY_POLL_ATTEMPTS - 1) * REGISTRY_POLL_INTERVAL_S of sleep.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

CONFIG = Path(__file__).with_name("data_agent.json")
REGISTRY_HOST = "https://agentregistry.googleapis.com/v1alpha"
# Compared in poll_operation. operations.get is one HTTP call bounded by
# CURL_MAX_TIME; this cap is what stops the wait.
REGISTRY_POLL_ATTEMPTS = 5
REGISTRY_POLL_INTERVAL_S = 2
CARD_LIMIT_BYTES = 10 * 1024
_SERVICE_ID = re.compile(r"^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$")


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
            "That id cannot be reused before then. Point ENV_INTELLIGENCE_AGENT "
            "or DATA_AGENT_ID at a live data agent."
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


def card_urls(location: str, name: str) -> list[str]:
    """v1 is the documented card. v1beta is the version createSync uses."""
    host = f"https://geminidataanalytics.{quote(location, safe='')}.rep.googleapis.com"
    return [
        f"{host}/v1/a2a/{name}/v1/card",
        f"{host}/v1beta/a2a/{name}/v1/card",
    ]


def check_registry_location(location: str) -> str:
    """Manual registration is rejected in multi-regions us and eu."""
    if location in {"us", "eu"}:
        raise DataAgentError(
            "Agent Registry manual registration is not supported in "
            f"multi-regions us and eu. Got {location}. Set "
            "DATA_AGENT_REGISTRY_LOCATION to global or a region such as "
            "us-central1."
        )
    return location


def check_service_id(service_id: str) -> str:
    if _SERVICE_ID.match(service_id) and 4 <= len(service_id) <= 63:
        return service_id
    raise DataAgentError(
        f"DATA_AGENT_ID {service_id!r} is not a valid Agent Registry service id. "
        "Use 4-63 characters matching ^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$."
    )


def registry_service_name(project: str, location: str, service_id: str) -> str:
    return (
        f"projects/{quote(project, safe='')}"
        f"/locations/{quote(location, safe='')}"
        f"/services/{quote(service_id, safe='')}"
    )


def service_display_name(card: dict, agent_id: str) -> str:
    name = card.get("name")
    if isinstance(name, str) and 1 <= len(name) <= 63:
        return name
    return agent_id


def registry_service_body(card: dict, agent_id: str) -> dict:
    size = len(json.dumps(card, separators=(",", ":")).encode())
    if size > CARD_LIMIT_BYTES:
        raise DataAgentError(
            f"A2A card is {size} bytes; Agent Registry accepts {CARD_LIMIT_BYTES}"
        )
    body = {
        "displayName": service_display_name(card, agent_id),
        "agentSpec": {"type": "A2A_AGENT_CARD", "content": card},
    }
    description = card.get("description")
    if isinstance(description, str) and description:
        body["description"] = description[:2048]
    return body


def accept_card(status: int, payload: dict) -> bool:
    if status != 200 or "error" in payload:
        return False
    return any(
        key in payload
        for key in ("name", "protocolVersion", "skills", "supportedInterfaces")
    )


def classify_registry_get(status: int, payload: dict, name: str) -> str:
    """'exists' or 'missing'."""
    if status == 404:
        return "missing"
    if status != 200:
        raise DataAgentError(f"GET {name} returned HTTP {status}\n{_dump(payload)}")
    return "exists"


def classify_registry_create(status: int, payload: dict) -> str:
    """'exists', 'pending', or 'done'. create returns an Operation.

    A body whose name is already a service is the created resource, not an
    operation to poll.
    """
    if status == 409:
        return "exists"
    if status not in (200, 201) or not payload.get("name"):
        raise DataAgentError(
            f"services.create returned HTTP {status}\n{_dump(payload)}"
        )
    message = _operation_error(payload)
    if message:
        raise DataAgentError(f"Agent Registry operation failed: {message}")
    name = payload["name"]
    if payload.get("done") is True or (
        "/services/" in name and "/operations/" not in name
    ):
        return "done"
    return "pending"


def _operation_error(payload: dict) -> str:
    error = payload.get("error")
    if not error:
        return ""
    if isinstance(error, dict):
        return error.get("message") or _dump(payload)
    return str(error)


def classify_operation(status: int, payload: dict) -> str:
    """'pending' or 'done'."""
    if status != 200:
        raise DataAgentError(
            f"operations.get returned HTTP {status}\n{_dump(payload)}"
        )
    message = _operation_error(payload)
    if message:
        raise DataAgentError(f"Agent Registry operation failed: {message}")
    if payload.get("done") is True:
        return "done"
    return "pending"


def operation_url(name: str) -> str:
    if name.startswith("https://"):
        return name
    return f"{REGISTRY_HOST}/{name.lstrip('/')}"


def registry_resource_name(payload: dict) -> str:
    response = payload.get("response")
    if not isinstance(response, dict):
        response = payload
    resource = response.get("registryResource") or ""
    if not isinstance(resource, str):
        return ""
    return resource


def poll_operation(fetch, sleep, attempts: int = REGISTRY_POLL_ATTEMPTS) -> dict:
    for attempt in range(1, attempts + 1):
        status, payload = fetch()
        if classify_operation(status, payload) == "done":
            return payload
        if attempt == attempts:
            raise DataAgentError(
                f"Agent Registry operation still running after {attempts} polls"
            )
        sleep(REGISTRY_POLL_INTERVAL_S)


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


def parse_data_agent_name(resource: str) -> tuple[str, str]:
    """(location, id) from a dataAgents resource name."""
    loc_marker = "/locations/"
    id_marker = "/dataAgents/"
    if loc_marker not in resource or id_marker not in resource:
        raise DataAgentError(
            f"ENV_INTELLIGENCE_AGENT {resource!r} is not a data agent resource name"
        )
    location = resource.split(loc_marker, 1)[1].split("/", 1)[0]
    agent_id = resource.split(id_marker, 1)[1].strip("/")
    if not location or not agent_id or "/" in agent_id:
        raise DataAgentError(
            f"ENV_INTELLIGENCE_AGENT {resource!r} is not a data agent resource name"
        )
    return location, agent_id


def agent_target() -> tuple[str, str]:
    """(location, id).

    DATA_AGENT_LOCATION and DATA_AGENT_ID win when set. Otherwise both come
    from ENV_INTELLIGENCE_AGENT. A fresh project with neither set uses us and
    environment-intelligence.
    """
    resource = os.environ.get("ENV_INTELLIGENCE_AGENT", "").strip()
    location = os.environ.get("DATA_AGENT_LOCATION", "").strip()
    agent_id = os.environ.get("DATA_AGENT_ID", "").strip()
    if resource and (not location or not agent_id):
        parsed_location, parsed_id = parse_data_agent_name(resource)
        location = location or parsed_location
        agent_id = agent_id or parsed_id
    return location or "us", agent_id or "environment-intelligence"


def _project() -> str:
    project = os.environ.get("PROJECT", "").strip() or os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    if not project:
        raise DataAgentError("set PROJECT or GOOGLE_CLOUD_PROJECT")
    return project


def ensure_data_agent(project: str, token: str, timeout: float) -> tuple[str, str, str]:
    dataset = os.environ.get("DATASET", "").strip() or os.environ.get("BQ_DATASET", "").strip() or "ci_agent_eval"
    location, agent_id = agent_target()
    name = resource_name(project, location, agent_id)
    host = endpoint(location)
    parent = name.rsplit("/dataAgents/", 1)[0]
    status, payload = _call("GET", f"{host}/{name}", token, project, timeout)
    if classify_get(status, payload, name) == "exists":
        print(f"Data agent already exists: {name}")
    else:
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
    return name, agent_id, location


def fetch_agent_card(location: str, name: str, token: str, project: str, timeout: float) -> dict:
    errors = []
    urls = card_urls(location, name)
    for index, url in enumerate(urls):
        status, payload = _call("GET", url, token, project, timeout)
        if accept_card(status, payload):
            return payload
        errors.append(f"GET {url} returned HTTP {status}\n{_dump(payload)}")
        if status != 404 or index == len(urls) - 1:
            break
    raise DataAgentError("\n".join(errors))


def ensure_registry(project: str, data_agent: str, agent_id: str, data_location: str, token: str, timeout: float) -> None:
    registry_location = check_registry_location(
        os.environ.get("DATA_AGENT_REGISTRY_LOCATION", "").strip() or "global"
    )
    service_id = check_service_id(agent_id)
    if data_location in {"us", "eu"}:
        print(
            f"Data agent location {data_location} does not accept Agent Registry "
            f"manual registration. The service is in {registry_location}."
        )
    service = registry_service_name(project, registry_location, service_id)
    status, payload = _call(
        "GET", f"{REGISTRY_HOST}/{service}", token, project, timeout
    )
    if classify_registry_get(status, payload, service) == "exists":
        print(f"Agent Registry service already exists: {service}")
        resource = registry_resource_name(payload)
        if resource:
            print(f"AGENT_REGISTRY_RESOURCE={resource}")
        return

    card = fetch_agent_card(data_location, data_agent, token, project, timeout)
    print(f"Registering {service}")
    parent = service.rsplit("/services/", 1)[0]
    status, payload = _call(
        "POST",
        f"{REGISTRY_HOST}/{parent}/services?serviceId={quote(service_id, safe='')}",
        token,
        project,
        timeout,
        registry_service_body(card, service_id),
    )
    action = classify_registry_create(status, payload)
    if action == "exists":
        print(f"Agent Registry service already exists: {service}")
        return
    if action == "pending":
        operation = payload["name"]
        payload = poll_operation(
            lambda: _call("GET", operation_url(operation), token, project, timeout),
            time.sleep,
        )
    resource = registry_resource_name(payload)
    if not resource:
        status, payload = _call(
            "GET", f"{REGISTRY_HOST}/{service}", token, project, timeout
        )
        classify_registry_get(status, payload, service)
        resource = registry_resource_name(payload) or service
    print(f"AGENT_REGISTRY_RESOURCE={resource}")


def run() -> None:
    project = _project()
    timeout = float(os.environ.get("CURL_MAX_TIME", "").strip() or "120")
    token = _required("DATA_AGENT_TOKEN")
    data_agent, agent_id, location = ensure_data_agent(project, token, timeout)
    ensure_registry(project, data_agent, agent_id, location, token, timeout)


def main() -> int:
    try:
        run()
    except DataAgentError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
