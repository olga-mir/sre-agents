"""The harness refuses to guess which agent it serves."""

import pytest

from agent_serving.reasoning_engine import _agent_module


def test_agent_module_has_no_default(monkeypatch):
    monkeypatch.delenv("AGENT_MODULE", raising=False)
    with pytest.raises(RuntimeError, match="AGENT_MODULE is not set"):
        _agent_module()


def test_blank_agent_module_is_unset(monkeypatch):
    monkeypatch.setenv("AGENT_MODULE", "   ")
    with pytest.raises(RuntimeError, match="AGENT_MODULE is not set"):
        _agent_module()


def test_agent_module_is_imported_by_name(monkeypatch):
    monkeypatch.setenv("AGENT_MODULE", "agent_serving.otel_auth")
    assert _agent_module().__name__ == "agent_serving.otel_auth"
