"""Invoke the deployed e2e-agent on Agent Runtime via AdkApp stream_query."""

import json
import os
import sys

import httpx
from google.genai import errors


def format_event(event: object) -> str:
    if isinstance(event, dict):
        text = json.dumps(event)
    else:
        text = str(event)
    return text[:500]


def stream_closed(exc: BaseException) -> bool:
    """The runtime scales to zero when the request ends and cuts the SSE body.

    google.genai then raises UnknownApiResponseError on the leftover partial
    chunk, or httpx.TransportError if the socket closes. An API error payload
    is a different class and is not a close.
    """
    if isinstance(exc, errors.UnknownApiResponseError):
        return True
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, (ConnectionError, BrokenPipeError, TimeoutError))


def stream_exit(exc: BaseException | None, saw_event: bool) -> int:
    """0 when the stream finished or closed after events. 1 on an API error."""
    if exc is None:
        return 0
    if isinstance(exc, errors.APIError):
        return 1
    if saw_event and stream_closed(exc):
        return 0
    return 1


def consume(stream) -> int:
    saw_event = False
    try:
        for event in stream:
            saw_event = True
            print(format_event(event))
    except Exception as exc:
        code = stream_exit(exc, saw_event)
        note = str(exc)
        if len(note) > 500:
            note = note[:500]
        if code == 0:
            print(f"stream closed after events: {note}", file=sys.stderr)
        else:
            print(note, file=sys.stderr)
        return code
    return 0


def main() -> int:
    import agentplatform

    display_name = os.environ.get("AGENT_DISPLAY_NAME", "e2e-agent")
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    if not project_id:
        raise SystemExit("set GOOGLE_CLOUD_PROJECT")
    agent_location = os.environ.get("AGENT_LOCATION", "us-central1")
    run_id = os.environ.get("RUN", "33259998849")
    repo = os.environ.get("REPO", "agent-substrate/substrate")
    engine_id = os.environ.get("ENGINE_ID")

    client = agentplatform.Client(project=project_id, location=agent_location)
    if engine_id:
        engine_name = engine_id
    else:
        engine_name = ""
        for runtime in client.runtimes.list():
            if getattr(runtime.api_resource, "display_name", None) == display_name:
                engine_name = runtime.api_resource.name
                break
        if not engine_name:
            raise SystemExit(
                f"no Agent Runtime named {display_name}; "
                "pass ENGINE_ID=projects/.../reasoningEngines/..."
            )

    print(f"Invoking remote agent {engine_name} ({display_name})...")
    agent = client.runtimes.get(name=engine_name)
    payload = {"trigger_source": "TEST_OPERATOR", "repo": repo, "run_id": run_id}
    return consume(
        agent.stream_query(user_id="task-invoke-agent", message=json.dumps(payload))
    )


if __name__ == "__main__":
    raise SystemExit(main())
