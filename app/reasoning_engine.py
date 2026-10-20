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

"""Serve the reasoning_engine ``{class_method, input}`` contract over HTTP.

Agent Engine forwards ``:query`` to ``/api/reasoning_engine`` and
``:streamQuery`` to ``/api/stream_reasoning_engine``. Dispatch is limited to
``AdkApp.register_operations()``, which is the same list ``agents-cli deploy``
records as ``class_methods`` for ``framework: google-adk``.

The wait inside a query is the investigation itself. Its deadlines are the
pinned budgets (``E2E_AGENT_MAX_TURNS``, ``ADK_MAX_LLM_CALLS``, and the
per-call model and warehouse timeouts), not a second socket timeout around
the whole run.
"""

from __future__ import annotations

import functools
import importlib
import inspect
import json
import os

from agentplatform.frameworks import AdkApp
from fastapi import FastAPI, HTTPException, Request, encoders, responses
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool


def _agent_module():
    name = os.environ.get("SRE_AGENT_MODULE", "app.agent").strip() or "app.agent"
    return importlib.import_module(name)


@functools.cache
def get_session_service():
    """Session service for this process.

    Agent Runtime injects ``GOOGLE_CLOUD_AGENT_ENGINE_ID`` in the container.
    ``GOOGLE_CLOUD_LOCATION`` is pinned to ``global`` for Gemini, so it is
    not a session region. The engine region is
    ``GOOGLE_CLOUD_AGENT_ENGINE_LOCATION``, then ``AGENT_LOCATION``.
    """
    agent_engine_id = os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_ID")
    if not agent_engine_id:
        from google.adk.sessions.in_memory_session_service import (
            InMemorySessionService,
        )

        return InMemorySessionService()

    try:
        from google.adk.sessions.vertex_ai_session_service import (
            VertexAiSessionService,
        )
    except ImportError:
        from google.adk.sessions.vertex_ai_session_service_g3 import (
            VertexAiSessionService,
        )

    location = (
        os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_LOCATION")
        or os.environ.get("AGENT_LOCATION")
        or "us-central1"
    )
    return VertexAiSessionService(
        project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
        location=location,
        agent_engine_id=agent_engine_id,
    )


@functools.cache
def get_artifact_service():
    bucket = os.environ.get("LOGS_BUCKET_NAME")
    if bucket:
        from google.adk.artifacts import GcsArtifactService

        return GcsArtifactService(bucket_name=bucket)
    from google.adk.artifacts.in_memory_artifact_service import (
        InMemoryArtifactService,
    )

    return InMemoryArtifactService()


def build_runtime() -> tuple[AdkApp, set[str], set[str]]:
    """Construct AdkApp and the method sets the HTTP routes may call.

    ``instrumentor_builder`` is left unset. ``AdkApp.set_up`` installs the
    default GenAI instrumentor only when that argument is absent. A no-op
    builder would skip it, and the completion upload would not run.
    """
    runtime = AdkApp(
        app=_agent_module().app,
        session_service_builder=get_session_service,
        artifact_service_builder=get_artifact_service,
    )
    runtime.set_up()
    operations = runtime.register_operations()
    streaming = set(operations.get("stream", [])) | set(
        operations.get("async_stream", [])
    )
    sync = set(operations.get("", [])) | set(operations.get("async", []))
    return runtime, streaming, sync


def attach_reasoning_engine_routes(app: FastAPI) -> None:
    """Register the two routes Agent Engine proxies to."""

    def runtime() -> AdkApp:
        return app.state.runtime

    def resolve_method(class_method: str, *, streaming: bool):
        allowed = app.state.streaming_methods if streaming else app.state.sync_methods
        if class_method not in allowed:
            raise HTTPException(
                status_code=404,
                detail=f"Unsupported reasoning_engine method: {class_method!r}",
            )
        return getattr(runtime(), class_method)

    @app.post("/api/stream_reasoning_engine")
    async def stream_query(request: Request) -> responses.StreamingResponse:
        body = await request.json()
        class_method = body.get("class_method")
        if not isinstance(class_method, str) or not class_method:
            raise HTTPException(status_code=400, detail="class_method is required")
        method = resolve_method(class_method, streaming=True)
        kwargs = body.get("input") or {}
        stream = (
            await method(**kwargs)
            if inspect.iscoroutinefunction(method)
            else method(**kwargs)
        )

        async def generator():
            if hasattr(stream, "__aiter__"):
                async for event in stream:
                    yield json.dumps(encoders.jsonable_encoder(event)) + "\n"
            else:
                async for event in iterate_in_threadpool(stream):
                    yield json.dumps(encoders.jsonable_encoder(event)) + "\n"

        return responses.StreamingResponse(
            content=generator(), media_type="application/json"
        )

    @app.post("/api/reasoning_engine")
    async def query(request: Request) -> responses.JSONResponse:
        body = await request.json()
        class_method = body.get("class_method")
        if not isinstance(class_method, str) or not class_method:
            raise HTTPException(status_code=400, detail="class_method is required")
        method = resolve_method(class_method, streaming=False)
        kwargs = body.get("input") or {}
        if inspect.iscoroutinefunction(method):
            output = await method(**kwargs)
        else:
            output = await run_in_threadpool(method, **kwargs)
        return responses.JSONResponse(
            content=encoders.jsonable_encoder({"output": output})
        )
