"""Deploy the sre_coordinator agent to Agent Runtime (display_name: 'sre-coordinator').

Local source bundle. Deployment is idempotent: if an agent with display_name
'sre-coordinator' already exists (or ENGINE_ID is set), it updates that engine
in place.

Needs a GCS staging bucket for the local source upload (STAGING_BUCKET env,
default gs://<project>-agent-staging) - create it first if it does not exist:
  gcloud storage buckets create gs://<project>-agent-staging --location=us-central1
"""

import os
import sys
from pathlib import Path
from typing import Optional

import agentplatform
from agentplatform.frameworks import AdkApp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agents.sre_coordinator.agent import root_agent  # noqa: E402

DISPLAY_NAME = os.environ.get("AGENT_DISPLAY_NAME", "sre-coordinator")
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
if not PROJECT_ID:
    sys.exit("set GOOGLE_CLOUD_PROJECT")
AGENT_LOCATION = os.environ.get("AGENT_LOCATION", "us-central1")
STAGING_BUCKET = os.environ.get("STAGING_BUCKET", f"gs://{PROJECT_ID}-agent-staging")
ENGINE_ID = os.environ.get("ENGINE_ID") or os.environ.get("SRE_COORDINATOR_ENGINE_ID")

client = agentplatform.Client(project=PROJECT_ID, location=AGENT_LOCATION)

# Rebuilding AdkApp here (rather than importing a prebuilt one, as
# ci_agent.agent.agent does for e2e-agent) because sre_coordinator exposes a
# plain Agent, not an App - AdkApp(agent=...) wraps it the same way.
adk_app = AdkApp(agent=root_agent)

config = {
    "staging_bucket": STAGING_BUCKET,
    # Pinned to the versions that cloudpickle this AdkApp and unpickle it in
    # the container. otel-gcp pulls in opentelemetry-instrumentation-google-genai.
    "requirements": [
        "google-adk[a2a,otel-gcp]==2.8.0",
        "google-cloud-aiplatform[agent_engines]==2.2.0",
        "pydantic==2.13.5",
        "cloudpickle==3.1.2",
    ],
    "extra_packages": ["agents/sre_coordinator"],
    "agent_framework": "google-adk",
    "display_name": DISPLAY_NAME,
    "identity_type": agentplatform.types.IdentityType.AGENT_IDENTITY,
    "env_vars": {
        # GOOGLE_CLOUD_PROJECT is reserved - Agent Runtime injects it itself
        # and a 400 FAILED_PRECONDITION rejects the deploy if it's set here.
        "AGENT_LOCATION": AGENT_LOCATION,
        "CI_AGENT_MODEL": os.environ.get("CI_AGENT_MODEL", "gemini-3.7-flash"),
        "E2E_AGENT_ENGINE": os.environ.get("E2E_AGENT_ENGINE", "").strip() or sys.exit("set E2E_AGENT_ENGINE"),
        "ENV_INTELLIGENCE_AGENT": os.environ.get("ENV_INTELLIGENCE_AGENT", "").strip() or sys.exit("set ENV_INTELLIGENCE_AGENT"),
        "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY": "true",
        "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
        # Message content stays on the span. e2e-agent uploads bodies to Cloud Storage.
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "SPAN_ONLY",
    },
}


def find_existing_engine() -> Optional[str]:
    if ENGINE_ID:
        return ENGINE_ID
    for r in client.runtimes.list():
        if getattr(r.api_resource, "display_name", None) == DISPLAY_NAME:
            return r.api_resource.name
    return None


def _print_identity(remote_agent) -> None:
    spec = getattr(remote_agent.api_resource, "spec", None)
    identity = getattr(spec, "effective_identity", None)
    if identity:
        member = identity if str(identity).startswith("principal://") else f"principal://{identity}"
        print(f"Effective identity: {identity}")
        print(f"IAM member: {member}")
        print("Grant this principal roles/aiplatform.user (for the e2e-agent "
              "stream_query call) and the BigQuery data agent role on first deploy.")


target_engine_id = find_existing_engine()

if target_engine_id:
    print(
        f"Updating existing agent {target_engine_id} ({DISPLAY_NAME}) "
        f"in place on Agent Runtime ({AGENT_LOCATION})..."
    )
    remote_agent = client.runtimes.update(name=target_engine_id, agent=adk_app, config=config)
    print(f"Agent Runtime updated successfully: {remote_agent.api_resource.name}")
else:
    print(f"Deploying new agent {DISPLAY_NAME} to Agent Runtime ({AGENT_LOCATION})...")
    remote_agent = client.runtimes.create(agent=adk_app, config=config)
    print(f"Agent Runtime deployed successfully: {remote_agent.api_resource.name}")

_print_identity(remote_agent)
