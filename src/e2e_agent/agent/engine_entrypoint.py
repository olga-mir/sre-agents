"""AdkApp for the investigation graph.

Agent Runtime starts `uvicorn app.fast_api_app:app` (see Dockerfile).
That process builds the same `AdkApp` and serves `register_operations()`
on the reasoning-engine routes. This module remains so a caller can
import `agent_engine` without starting the HTTP server.

Gemini Enterprise registration stays `--registration-type adk`. An A2A
entrypoint is `agent_framework: custom` on this API and drops the ADK
console surface.
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
