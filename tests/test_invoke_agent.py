"""The invoke client exits 0 when the runtime cuts the stream after events."""

import importlib.util
from pathlib import Path

import httpx
from google.genai import errors

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "invoke_agent", ROOT / "deploy" / "invoke_agent.py"
)
invoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(invoke)


def _events_then(exc):
    yield {"author": "investigator", "text": "found it"}
    raise exc


def test_finished_stream_exits_zero():
    assert invoke.consume([{"text": "ok"}]) == 0


def test_api_error_after_events_exits_one(capsys):
    err = errors.ClientError(400, {"error": {"message": "bad request"}})
    assert invoke.consume(_events_then(err)) == 1
    captured = capsys.readouterr()
    assert "found it" in captured.out
    assert "bad request" in captured.err


def test_truncated_chunk_after_events_exits_zero(capsys):
    err = errors.UnknownApiResponseError("Failed to parse response as JSON")
    assert invoke.consume(_events_then(err)) == 0
    captured = capsys.readouterr()
    assert "found it" in captured.out
    assert "stream closed after events" in captured.err


def test_truncated_chunk_before_events_exits_one():
    def empty():
        raise errors.UnknownApiResponseError("partial")
        yield  # pragma: no cover

    assert invoke.consume(empty()) == 1


def test_socket_close_after_events_exits_zero():
    err = httpx.RemoteProtocolError("peer closed connection")
    assert invoke.consume(_events_then(err)) == 0
