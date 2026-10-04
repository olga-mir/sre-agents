"""TriggerEvent parsing for Gemini Enterprise / A2A chat text."""

from ci_agent.agent.trigger_parse import parse_trigger_text


def test_parses_trigger_event_json():
    event = parse_trigger_text(
        '{"trigger_source":"TEST_OPERATOR","repo":"o/r","run_id":"99"}'
    )
    assert event is not None
    assert event.repo == "o/r" and event.run_id == "99"


def test_parses_github_actions_url():
    event = parse_trigger_text(
        "please look at https://github.com/agent-substrate/substrate/actions/runs/33259998849"
    )
    assert event is not None
    assert event.repo == "agent-substrate/substrate"
    assert event.run_id == "33259998849"


def test_parses_prose_with_repo_and_run():
    event = parse_trigger_text("investigate agent-substrate/substrate run 33259998849")
    assert event is not None
    assert event.repo == "agent-substrate/substrate"
    assert event.run_id == "33259998849"


def test_uses_default_repo_when_only_run_id_is_present(monkeypatch):
    monkeypatch.delenv("CI_AGENT_REPO", raising=False)
    event = parse_trigger_text("run 33259998849", default_repo="o/r")
    assert event is not None
    assert event.repo == "o/r" and event.run_id == "33259998849"


def test_uses_ci_agent_repo_env(monkeypatch):
    monkeypatch.setenv("CI_AGENT_REPO", "env/repo")
    event = parse_trigger_text("run 33259998849")
    assert event is not None
    assert event.repo == "env/repo"


def test_returns_none_without_run_id():
    assert parse_trigger_text("look at agent-substrate/substrate") is None


def test_returns_none_for_empty():
    assert parse_trigger_text("") is None
    assert parse_trigger_text("   ") is None
