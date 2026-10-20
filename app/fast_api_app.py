# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Process ``agents-cli deploy`` starts inside the Agent Runtime container.

``SRE_AGENT_MODULE`` selects the ADK App. It defaults to ``app.agent``
(e2e-agent). ``task deploy AGENT=sre-coordinator`` sets
``agents.sre_coordinator.runtime``. Gemini Enterprise stays on ADK
``:streamQuery``. This process does not serve an A2A card.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator

try:
    import certifi

    if "SSL_CERT_FILE" not in os.environ:
        os.environ["SSL_CERT_FILE"] = certifi.where()
except ImportError:
    pass

from fastapi import FastAPI

from app.reasoning_engine import attach_reasoning_engine_routes, build_runtime


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    runtime, streaming, sync = build_runtime()
    app.state.runtime = runtime
    app.state.streaming_methods = streaming
    app.state.sync_methods = sync
    yield


app = FastAPI(title="sre-agents", lifespan=lifespan)
attach_reasoning_engine_routes(app)


@app.get("/")
async def health() -> dict[str, str]:
    return {"status": "ok"}
