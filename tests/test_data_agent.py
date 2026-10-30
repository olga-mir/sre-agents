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
    else:
        raise AssertionError("soft-deleted agent was treated as live")


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
