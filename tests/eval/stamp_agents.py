"""Write agent_data.agents onto an eval trace before `agents-cli eval grade`.

`eval generate` fills that map from ADK `GET /apps/{app}/app-info`. The route
only walks an LlmAgent root, and this app's root is the sre_investigation
Workflow, so the trace is saved with turns and no tool catalog. Trajectory
and tool-use metrics then judge an empty catalog.

The declarations already exist on investigator_agent and deep_dive_agent.
This copies them. The map key is the agent name, which is also the event
author (`investigator`, `deep_dive`).

    .venv/bin/python tests/eval/stamp_agents.py artifacts/traces/<trace>.json

Then grade that same file. Re-run this after every `eval generate`; a new
trace file does not carry the map.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from google.adk.utils.agent_info import get_tools_info

from ci_agent.agent.nodes import deep_dive_agent, investigator_agent

# Event authors on a generated trace. Not Workflow nodes: those are
# FunctionNodes, and the metrics look up tools by the LLM event author.
_LLM_AGENTS = (investigator_agent, deep_dive_agent)


async def _declarations(agent) -> list[dict]:
    tools = await get_tools_info(list(agent.tools))
    return [tool.model_dump(mode="json", exclude_none=True) for tool in tools]


async def _build_agents_map() -> dict[str, dict]:
    declared = await asyncio.gather(
        *(_declarations(agent) for agent in _LLM_AGENTS)
    )
    catalog = {}
    for agent, tools in zip(_LLM_AGENTS, declared, strict=True):
        entry = {
            "agent_id": agent.name,
            "agent_type": type(agent).__name__,
            "instruction": agent.instruction,
            "tools": tools,
        }
        description = getattr(agent, "description", None)
        if description:
            entry["description"] = description
        catalog[agent.name] = entry
    return catalog


def agents_map() -> dict[str, dict]:
    """Catalog the eval service reads as System Tool Context."""
    return asyncio.run(_build_agents_map())


def stamp_document(document: dict, catalog: dict[str, dict] | None = None) -> int:
    """Set agent_data.agents on every eval case. Returns the case count."""
    catalog = agents_map() if catalog is None else catalog
    cases = document.get("eval_cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("trace has no eval_cases")
    stamped = 0
    for case in cases:
        agent_data = case.get("agent_data")
        if not isinstance(agent_data, dict):
            raise ValueError(
                f"case {case.get('eval_case_id')!r} has no agent_data.turns"
            )
        agent_data["agents"] = catalog
        stamped += 1
    return stamped


def tool_names(entry: dict) -> list[str]:
    names = []
    for tool in entry.get("tools") or []:
        for declaration in tool.get("function_declarations") or []:
            name = declaration.get("name")
            if name:
                names.append(name)
    return names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "trace",
        type=Path,
        help="Populated trace JSON from `agents-cli eval generate`.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Where to write. Defaults to overwriting the input trace.",
    )
    args = parser.parse_args(argv)
    destination = args.output or args.trace

    document = json.loads(args.trace.read_text())
    catalog = agents_map()
    count = stamp_document(document, catalog)
    destination.write_text(json.dumps(document, indent=2) + "\n")

    print(f"stamped {count} case(s) -> {destination}")
    for agent_id, entry in catalog.items():
        names = ", ".join(tool_names(entry))
        print(f"  {agent_id}: {len(tool_names(entry))} tools ({names})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
