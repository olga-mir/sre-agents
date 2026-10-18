"""Module-level object for the Developer-Connect source-based Agent Runtime
deploy (see deploy/deploy_agent.py).

That path takes an `entrypoint_module`/`entrypoint_object` pair and binds a
fixed `class_methods` list to whatever attribute that names. The raw ADK
`App`/`Workflow` does not implement query/session/stream; `AdkApp` does
(`register_operations()`). Keep this as `AdkApp` so Agent Runtime stays on
the `google-adk` framework: playground, traces, and Gemini Enterprise
`:streamQuery` registration.

Do not swap this for `A2aAgent`. That object is `agent_framework: custom`
on this API, which drops the ADK console surface, and A2A registration
then needs an Agent Card URL that source-based Agent Runtime does not
hand to `agents-cli`.
"""

import os

try:
    import certifi

    if "SSL_CERT_FILE" not in os.environ:
        os.environ["SSL_CERT_FILE"] = certifi.where()
except ImportError:
    pass

from agentplatform.frameworks import AdkApp

from .agent import app

agent_engine = AdkApp(app=app)
