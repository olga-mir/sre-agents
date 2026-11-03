"""SourceStore backed by ADK artifacts.

ADK's Context (ToolContext is an alias of it in this version -
tools/tool_context.py:26) exposes async save_artifact/load_artifact, which is
the only durable place a tool may leave bytes: ResumabilityConfig is explicit
that in-memory state is lost on resumption.

save_artifact unconditionally creates version N+1 even for identical bytes, so
writes are non-corrupting but NOT deduplicated - hence the read-before-write.
"""

from __future__ import annotations

from typing import Any, Optional

MIME = "text/plain"


class ArtifactStore:
    def __init__(self, tool_context: Any):
        self._ctx = tool_context

    async def get(self, key: str) -> Optional[bytes]:
        part = await self._ctx.load_artifact(key)
        if part is None:
            return None
        blob = getattr(part, "inline_data", None)
        return getattr(blob, "data", None) if blob is not None else None

    async def put(self, key: str, data: bytes) -> None:
        if await self.get(key) is not None:
            return  # a second save would burn a version for identical bytes
        from google.genai import types  # imported lazily: ADK is an extra

        await self._ctx.save_artifact(
            key, types.Part.from_bytes(data=data, mime_type=MIME)
        )
