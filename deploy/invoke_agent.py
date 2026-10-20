"""Invoke the deployed e2e-agent on Agent Runtime via AdkApp stream_query."""

import json
import os

import agentplatform

DISPLAY_NAME = os.environ.get("AGENT_DISPLAY_NAME", "e2e-agent")
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
if not PROJECT_ID:
    raise SystemExit("set GOOGLE_CLOUD_PROJECT")
AGENT_LOCATION = os.environ.get("AGENT_LOCATION", "us-central1")
RUN_ID = os.environ.get("RUN", "33259998849")
REPO = os.environ.get("REPO", "agent-substrate/substrate")
ENGINE_ID = os.environ.get("ENGINE_ID")

client = agentplatform.Client(project=PROJECT_ID, location=AGENT_LOCATION)

def find_target_engine() -> str:
    if ENGINE_ID:
        return ENGINE_ID
    for r in client.runtimes.list():
        if getattr(r.api_resource, "display_name", None) == DISPLAY_NAME:
            return r.api_resource.name
    raise SystemExit(
        f"no Agent Runtime named {DISPLAY_NAME}; "
        "pass ENGINE_ID=projects/.../reasoningEngines/..."
    )

engine_name = find_target_engine()
print(f"Invoking remote agent {engine_name} ({DISPLAY_NAME})...")

agent = client.runtimes.get(name=engine_name)
payload = {"trigger_source": "TEST_OPERATOR", "repo": REPO, "run_id": RUN_ID}

for event in agent.stream_query(user_id="task-invoke-agent", message=json.dumps(payload)):
    if isinstance(event, dict):
        print(json.dumps(event)[:500])
    else:
        print(str(event)[:500])
