"""ADK App the container serves for the sre-coordinator engine.

``task deploy AGENT=sre-coordinator`` sets
``SRE_AGENT_MODULE=agents.sre_coordinator.runtime``. The module exports
``app`` and ``root_agent``, which is what ``app.reasoning_engine`` loads.
"""

from google.adk.apps import App

from agents.sre_coordinator.agent import root_agent

app = App(name="sre_coordinator", root_agent=root_agent)
