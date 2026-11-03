import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    path = ROOT / "deploy" / "data_agent.py"
    spec = importlib.util.spec_from_file_location("data_agent_apply", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


agent = _load()


def test_shell_script_does_not_embed_python():
    text = (ROOT / "deploy" / "data_agent.sh").read_text()
    assert "python3 -" not in text
    assert "<<" not in text
    assert "data_agent.py" in text
    assert "agentregistry.googleapis.com" in text
    assert 'DATA_AGENT_REGISTRY_LOCATION="${DATA_AGENT_REGISTRY_LOCATION:-global}"' in text
    assert "dotenv_value" in text
    assert 'DATA_AGENT_ID="${DATA_AGENT_ID:-environment-intelligence}"' not in text


def test_stamp_replaces_only_whole_sentinel_values():
    stamped = agent.stamp(
        {"datasetId": "DATASET_ID", "note": "use DATASET_ID here"},
        "ci_agent_eval",
    )
    assert stamped == {
        "datasetId": "ci_agent_eval",
        "note": "use DATASET_ID here",
    }


def test_config_has_no_project_id_and_the_request_adds_it():
    assert "projectId" not in (ROOT / "deploy" / "data_agent.json").read_text()
    body = agent.request_body(agent.load_config(), "sample-project", "ci_agent_eval")
    staging = body["dataAnalyticsAgent"]["stagingContext"]
    published = body["dataAnalyticsAgent"]["publishedContext"]
    assert published == staging
    refs = staging["datasourceReferences"]["bq"]["tableReferences"]
    assert [ref["tableId"] for ref in refs] == [
        "v_ci_health",
        "v_outcomes",
        "v_verdict_rates",
        "v_path_reach",
    ]
    assert {ref["projectId"] for ref in refs} == {"sample-project"}
    assert {ref["datasetId"] for ref in refs} == {"ci_agent_eval"}


def test_get_treats_soft_delete_as_unusable():
    name = "projects/p/locations/us/dataAgents/environment-intelligence"
    assert agent.classify_get(404, {}, name) == "missing"
    assert agent.classify_get(200, {"name": name}, name) == "exists"
    try:
        agent.classify_get(
            200,
            {"deleteTime": "2026-10-30T07:06:15Z", "purgeTime": "2026-11-29T07:06:15Z"},
            name,
        )
    except agent.DataAgentError as exc:
        assert "soft-deleted until 2026-11-29T07:06:15Z" in str(exc)
        assert (
            "Point ENV_INTELLIGENCE_AGENT or DATA_AGENT_ID at a live data agent."
            in str(exc)
        )
    else:
        raise AssertionError("soft-deleted agent was treated as live")


def test_agent_target_explicit_id_wins(monkeypatch):
    monkeypatch.setenv("DATA_AGENT_ID", "other-agent")
    monkeypatch.setenv("DATA_AGENT_LOCATION", "us-central1")
    monkeypatch.setenv(
        "ENV_INTELLIGENCE_AGENT",
        "projects/p/locations/us/dataAgents/environment-intelligence-2",
    )
    assert agent.agent_target() == ("us-central1", "other-agent")


def test_agent_target_defaults_when_unset(monkeypatch):
    monkeypatch.delenv("DATA_AGENT_ID", raising=False)
    monkeypatch.delenv("DATA_AGENT_LOCATION", raising=False)
    monkeypatch.delenv("ENV_INTELLIGENCE_AGENT", raising=False)
    assert agent.agent_target() == ("us", "environment-intelligence")


def test_agent_target_rejects_a_bad_resource(monkeypatch):
    monkeypatch.delenv("DATA_AGENT_ID", raising=False)
    monkeypatch.setenv("ENV_INTELLIGENCE_AGENT", "not-a-resource")
    try:
        agent.agent_target()
    except agent.DataAgentError as exc:
        assert str(exc) == (
            "ENV_INTELLIGENCE_AGENT 'not-a-resource' is not a data agent resource name"
        )
    else:
        raise AssertionError("bad resource was accepted")


def test_registry_location_rejects_multi_regions():
    message = (
        "Agent Registry manual registration is not supported in "
        "multi-regions us and eu. Got {location}. Set "
        "DATA_AGENT_REGISTRY_LOCATION to global or a region such as "
        "us-central1."
    )
    for location in ("us", "eu"):
        try:
            agent.check_registry_location(location)
        except agent.DataAgentError as exc:
            assert str(exc) == message.format(location=location)
        else:
            raise AssertionError(f"{location} was accepted")
    assert agent.check_registry_location("global") == "global"
    assert agent.check_registry_location("us-central1") == "us-central1"


def test_service_id_rejects_a_short_name():
    try:
        agent.check_service_id("ab")
    except agent.DataAgentError as exc:
        assert str(exc) == (
            "DATA_AGENT_ID 'ab' is not a valid Agent Registry service id. "
            "Use 4-63 characters matching ^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$."
        )
    else:
        raise AssertionError("short id was accepted")
    assert agent.check_service_id("environment-intelligence") == "environment-intelligence"


def test_registry_body_is_an_a2a_card_without_interfaces():
    card = {
        "name": "Environment intelligence",
        "description": "x" * 3000,
        "skills": [],
    }
    body = agent.registry_service_body(card, "environment-intelligence")
    assert body["agentSpec"] == {"type": "A2A_AGENT_CARD", "content": card}
    assert "interfaces" not in body
    assert body["displayName"] == "Environment intelligence"
    assert len(body["description"]) == 2048
    long_name = agent.registry_service_body(
        {"name": "n" * 64, "protocolVersion": "1.0"},
        "environment-intelligence",
    )
    assert long_name["displayName"] == "environment-intelligence"


def test_registry_body_rejects_a_card_over_10kb():
    card = {"name": "n", "blob": "a" * 11000}
    size = len(agent.json.dumps(card, separators=(",", ":")).encode())
    try:
        agent.registry_service_body(card, "environment-intelligence")
    except agent.DataAgentError as exc:
        assert str(exc) == (
            f"A2A card is {size} bytes; Agent Registry accepts 10240"
        )
    else:
        raise AssertionError("oversized card was accepted")


def test_registry_create_accepts_conflict_and_a_finished_operation():
    service = "projects/p/locations/global/services/environment-intelligence"
    operation = "projects/p/locations/global/operations/op1"
    assert agent.classify_registry_get(404, {}, service) == "missing"
    assert agent.classify_registry_get(200, {"name": service}, service) == "exists"
    assert agent.classify_registry_create(409, {"error": {"code": 409}}) == "exists"
    assert agent.classify_registry_create(
        200, {"name": operation, "done": False}
    ) == "pending"
    assert agent.classify_registry_create(
        200, {"name": operation, "done": True, "response": {}}
    ) == "done"
    assert agent.classify_registry_create(
        200, {"name": service, "registryResource": "projects/p/locations/global/agents/a"}
    ) == "done"
    try:
        agent.classify_operation(
            200, {"done": True, "error": {"message": "card rejected"}}
        )
    except agent.DataAgentError as exc:
        assert str(exc) == "Agent Registry operation failed: card rejected"
    else:
        raise AssertionError("operation error was ignored")


def test_poll_stops_at_the_attempt_cap():
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return 200, {"done": False, "name": "operations/1"}

    slept = []
    try:
        agent.poll_operation(fetch, slept.append, attempts=5)
    except agent.DataAgentError as exc:
        assert str(exc) == "Agent Registry operation still running after 5 polls"
    else:
        raise AssertionError("poll did not stop")
    assert calls["n"] == 5
    assert slept == [2, 2, 2, 2]


def test_card_falls_through_to_v1beta_only_on_404(monkeypatch):
    seen = []

    def fake_call(method, url, token, project, timeout, body=None):
        seen.append(url)
        if url.endswith("/v1/a2a/projects/p/locations/us/dataAgents/a/v1/card"):
            return 404, {}
        if url.endswith("/v1beta/a2a/projects/p/locations/us/dataAgents/a/v1/card"):
            return 200, {"name": "A", "skills": []}
        raise AssertionError(url)

    monkeypatch.setattr(agent, "_call", fake_call)
    card = agent.fetch_agent_card(
        "us", "projects/p/locations/us/dataAgents/a", "t", "p", 1
    )
    assert card == {"name": "A", "skills": []}
    assert [url.rsplit(".com", 1)[-1] for url in seen] == [
        "/v1/a2a/projects/p/locations/us/dataAgents/a/v1/card",
        "/v1beta/a2a/projects/p/locations/us/dataAgents/a/v1/card",
    ]

    def denied(method, url, token, project, timeout, body=None):
        return 403, {"error": {"message": "denied"}}

    monkeypatch.setattr(agent, "_call", denied)
    try:
        agent.fetch_agent_card(
            "us", "projects/p/locations/us/dataAgents/a", "t", "p", 1
        )
    except agent.DataAgentError as exc:
        assert "HTTP 403" in str(exc)
        assert "v1beta" not in str(exc)
    else:
        raise AssertionError("403 was ignored")


def test_existing_data_agent_registers_without_rewriting_the_service(monkeypatch):
    calls = []

    def fake_call(method, url, token, project, timeout, body=None):
        calls.append((method, url))
        if method == "GET" and url.endswith("/dataAgents/environment-intelligence"):
            return 200, {"name": "projects/p/locations/us/dataAgents/environment-intelligence"}
        if method == "GET" and url.endswith("/services/environment-intelligence"):
            return 200, {
                "name": "projects/p/locations/global/services/environment-intelligence",
                "registryResource": "projects/p/locations/global/agents/environment-intelligence",
            }
        raise AssertionError(f"{method} {url}")

    monkeypatch.setattr(agent, "_call", fake_call)
    monkeypatch.setenv("PROJECT", "p")
    monkeypatch.setenv("DATA_AGENT_TOKEN", "t")
    monkeypatch.setenv("DATA_AGENT_LOCATION", "us")
    monkeypatch.setenv("DATA_AGENT_ID", "environment-intelligence")
    monkeypatch.setenv("DATA_AGENT_REGISTRY_LOCATION", "global")
    agent.run()
    assert ("POST",) not in [(method,) for method, _url in calls]
    assert any(
        method == "GET" and url.endswith("/services/environment-intelligence")
        for method, url in calls
    )
    assert not any("/v1/card" in url for _method, url in calls)


def test_missing_service_is_created_from_the_card(monkeypatch):
    posts = []
    polls = {"n": 0}

    def fake_call(method, url, token, project, timeout, body=None):
        if method == "GET" and url.endswith("/dataAgents/environment-intelligence"):
            return 200, {"name": "projects/p/locations/us/dataAgents/environment-intelligence"}
        if method == "GET" and url.endswith("/services/environment-intelligence"):
            return 404, {}
        if url.endswith("/v1/a2a/projects/p/locations/us/dataAgents/environment-intelligence/v1/card"):
            return 200, {"name": "Env", "protocolVersion": "1.0"}
        if method == "POST" and "/services?" in url:
            posts.append(body)
            return 200, {
                "name": "projects/p/locations/global/operations/op1",
                "done": False,
            }
        if method == "GET" and url.endswith("/operations/op1"):
            polls["n"] += 1
            if polls["n"] < 2:
                return 200, {"name": "projects/p/locations/global/operations/op1", "done": False}
            return 200, {
                "done": True,
                "response": {
                    "registryResource": "projects/p/locations/global/agents/environment-intelligence",
                },
            }
        raise AssertionError(f"{method} {url}")

    monkeypatch.setattr(agent, "_call", fake_call)
    monkeypatch.setattr(agent.time, "sleep", lambda _seconds: None)
    monkeypatch.setenv("PROJECT", "p")
    monkeypatch.setenv("DATA_AGENT_TOKEN", "t")
    monkeypatch.setenv("DATA_AGENT_LOCATION", "us")
    monkeypatch.setenv("DATA_AGENT_ID", "environment-intelligence")
    monkeypatch.setenv("DATA_AGENT_REGISTRY_LOCATION", "global")
    agent.run()
    assert posts == [{
        "displayName": "Env",
        "agentSpec": {
            "type": "A2A_AGENT_CARD",
            "content": {"name": "Env", "protocolVersion": "1.0"},
        },
    }]
    assert polls["n"] == 2


def test_create_accepts_conflict_and_rejects_a_bad_name():
    name = "projects/p/locations/us/dataAgents/environment-intelligence"
    agent.classify_create(409, {"error": {"code": 409}}, name)
    agent.classify_create(200, {"name": name}, name)
    try:
        agent.classify_create(200, {"name": "not-a-data-agent"}, name)
    except agent.DataAgentError as exc:
        assert "not-a-data-agent" in str(exc)
    else:
        raise AssertionError("create returned a name that is not a data agent")
