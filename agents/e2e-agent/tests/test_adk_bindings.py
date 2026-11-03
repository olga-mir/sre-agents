import pytest

from e2e_agent.tools.adk_tools import _run_context
from e2e_agent.tools.artifact_store import ArtifactStore


class FakePart:
    def __init__(self, data: bytes):
        self.inline_data = type("Blob", (), {"data": data})()


class FakeContext:
    def __init__(self, artifacts=None, state=None):
        self.artifacts = artifacts or {}
        self.state = state or {}
        self.saves = 0

    async def load_artifact(self, filename, version=None):
        data = self.artifacts.get(filename)
        return FakePart(data) if data is not None else None

    async def save_artifact(self, filename, artifact, custom_metadata=None):
        self.saves += 1
        return self.saves


async def test_artifact_store_unwraps_inline_data():
    ctx = FakeContext({"ev/1/source_file/abc/x.go": b"package x"})
    assert await ArtifactStore(ctx).get("ev/1/source_file/abc/x.go") == b"package x"
    assert await ArtifactStore(ctx).get("missing") is None


async def test_put_does_not_burn_a_version_on_identical_bytes():
    """save_artifact always creates version N+1, so re-saving is wasteful."""
    ctx = FakeContext({"k": b"same"})
    await ArtifactStore(ctx).put("k", b"same")
    assert ctx.saves == 0


def test_tools_refuse_to_run_before_the_run_is_pinned():
    with pytest.raises(RuntimeError, match="pin_run_context"):
        _run_context(FakeContext(state={}))


def test_run_context_is_read_from_state():
    state = {"run_context": {"repo": "o/r", "run_id": "1", "head_sha": "a" * 40,
                             "base_sha": "b" * 40, "base_ref_name": "main"}}
    rc = _run_context(FakeContext(state=state))
    assert rc.repo == "o/r" and rc.known_shas == frozenset({"a" * 40, "b" * 40})
