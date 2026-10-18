"""Discovery shim for `adk web` / `adk run` / `adk api_server`.

The loader lists the directories under its target as agent names, so this exists
purely to make the agent show up as `e2e_agent` rather than `agent`. The graph
itself lives in e2e_agent.agent.agent.
"""

from e2e_agent.agent.agent import app, root_agent  # noqa: F401
