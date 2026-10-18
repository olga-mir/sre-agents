"""The stamp must publish the investigator catalog under the event author."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from e2e_agent.agent.nodes import deep_dive_agent, investigator_agent
from stamp_agents import agents_map, stamp_document, tool_names


def test_agents_map_keys_match_event_authors():
    catalog = agents_map()
    assert set(catalog) == {investigator_agent.name, deep_dive_agent.name}
    for agent in (investigator_agent, deep_dive_agent):
        entry = catalog[agent.name]
        assert entry["agent_id"] == agent.name
        assert entry["agent_type"] == "LlmAgent"
        assert entry["instruction"] == agent.instruction
        assert "find_in_log" in tool_names(entry)
        assert "read_symbols" in tool_names(entry)


def test_stamp_document_sets_agents_on_each_case():
    catalog = {"investigator": {"agent_id": "investigator", "tools": []}}
    document = {
        "eval_cases": [
            {"eval_case_id": "one", "agent_data": {"turns": []}},
        ]
    }
    assert stamp_document(document, catalog) == 1
    assert document["eval_cases"][0]["agent_data"]["agents"] is catalog
