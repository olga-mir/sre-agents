"""Deploy the e2e_agent graph to Agent Runtime (display_name: 'e2e-agent').

Deployment is idempotent: if an agent with display_name 'e2e-agent' already exists
(or if ENGINE_ID / DC_ENGINE_ID is explicitly provided), it updates the existing engine
in place rather than creating a new reasoningEngine.

The engine runs as an Agent Identity. After the first deploy that sets
identity_type, run deploy/grant_agent_identity_iam.sh. It grants the
printed principal secretAccessor on the GitHub App private-key secret,
roles/bigquery.dataEditor on ci_agent_eval, and roles/bigquery.jobUser.
Project setup before this script is deploy/provision.sh.
"""

import os
import sys
from typing import Optional

import agentplatform
from agentplatform.frameworks import AdkApp

from e2e_agent.agent.agent import app


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"set {name}")
    return value


DISPLAY_NAME = os.environ.get("AGENT_DISPLAY_NAME", "e2e-agent")
PROJECT_ID = _required("GOOGLE_CLOUD_PROJECT")
AGENT_LOCATION = os.environ.get("AGENT_LOCATION", "us-central1")
DC_LOCATION = os.environ.get("DC_LOCATION", "us-central1")
CONNECTION = os.environ.get("DC_CONNECTION", "gh-connect")
REPO_LINK = _required("DC_REPO_LINK")
REVISION = os.environ.get("DC_REVISION", "main")
ENGINE_ID = os.environ.get("ENGINE_ID") or os.environ.get("DC_ENGINE_ID")

client = agentplatform.Client(
    project=PROJECT_ID,
    location=AGENT_LOCATION,
    http_options={"api_version": "v1beta1"},
)

repo_link = (
    f"projects/{PROJECT_ID}/locations/{DC_LOCATION}/connections/{CONNECTION}"
    f"/gitRepositoryLinks/{REPO_LINK}"
)

# Local-only: class_methods must match AdkApp (stream_query / sessions).
operations = AdkApp(app=app).register_operations()
class_methods = [
    {"name": name, "api_mode": api_mode}
    for api_mode, names in operations.items()
    for name in names
]

config = {
    "developer_connect_source": {
        "git_repository_link": repo_link,
        "revision": REVISION,
        "dir": "src",
    },
    "entrypoint_module": "e2e_agent.agent.engine_entrypoint",
    "entrypoint_object": "agent_engine",
    "requirements_file": "requirements.txt",
    "class_methods": class_methods,
    "agent_framework": "google-adk",
    "display_name": DISPLAY_NAME,
    "identity_type": agentplatform.types.IdentityType.AGENT_IDENTITY,
    "env_vars": {
        "E2E_AGENT_REPO": "agent-substrate/substrate",
        "E2E_AGENT_MODEL": "gemini-3.7-flash",
        "E2E_AGENT_LOCATION": "global",
        "GOOGLE_CLOUD_LOCATION": "global",
        # Prompt and response bodies go to Cloud Storage. The log and the
        # trace keep a gs:// reference (gen_ai.input.messages_ref and
        # friends), not the text. NO_CONTENT is what keeps the body off the
        # span. The upload itself is done by
        # opentelemetry-instrumentation-google-genai, which src/requirements.txt
        # must include; Developer Connect installs that file from GitHub main.
        # https://docs.cloud.google.com/trace/docs/collect-view-multimodal-prompts-responses
        "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY": "true",
        "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "NO_CONTENT",
        "OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK": "upload",
        "OTEL_INSTRUMENTATION_GENAI_UPLOAD_FORMAT": "jsonl",
        "OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH": (
            f"gs://{os.environ.get('COMPLETIONS_BUCKET', PROJECT_ID + '-e2e-agent-completions')}/completions"
        ),
        "E2E_AGENT_MAX_TURNS": "3",
        # Caps total Gemini calls across the whole invocation (all nodes).
        "ADK_MAX_LLM_CALLS": "60",
        # Pinned, not left to the code default: a bound whose production value
        # is whatever src/ happens to default to is not a number anyone chose.
        # Client poll deadline AND job_timeout_ms for every warehouse MERGE.
        "E2E_AGENT_BQ_TIMEOUT_S": "30",
        # Backoff retries per Gemini call before investigator_loop takes the
        # routed quota_exhausted exit.
        "E2E_AGENT_MODEL_RETRY_ATTEMPTS": "6",
        "BQ_DATASET": "ci_agent_eval",
        "BQ_LOCATION": "us-central1",
        "E2E_AGENT_BQ_WRITE": "1",
        "GITHUB_APP_ID": _required("GITHUB_APP_ID"),
        "GITHUB_APP_INSTALLATION_ID": _required("GITHUB_APP_INSTALLATION_ID"),
        "GITHUB_APP_PRIVATE_KEY_SECRET": os.environ.get(
            "GITHUB_APP_PRIVATE_KEY_SECRET", "e2e-agent-github-app-private-key"
        ),
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


target_engine_id = find_existing_engine()

if target_engine_id:
    print(
        f"Updating existing agent {target_engine_id} ({DISPLAY_NAME}) "
        f"in place on Agent Runtime ({AGENT_LOCATION}) from {repo_link}@{REVISION}..."
    )
    remote_agent = client.runtimes.update(name=target_engine_id, config=config)
    print(f"Agent Runtime updated successfully: {remote_agent.api_resource.name}")
else:
    print(
        f"Deploying new agent {DISPLAY_NAME} to Agent Runtime ({AGENT_LOCATION}) "
        f"from {repo_link}@{REVISION}..."
    )
    remote_agent = client.runtimes.create(config=config)
    print(f"Agent Runtime deployed successfully: {remote_agent.api_resource.name}")

_print_identity(remote_agent)
