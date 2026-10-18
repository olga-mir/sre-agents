"""Discovery shim for `agents-cli playground` and `agents-cli run`.

Those commands serve `adk web .` / `adk api_server .` and load
`{agent_directory}` as a direct child of the repo root. `agents/e2e_agent`
is the `adk web agents` entry so the UI shows `e2e_agent`.
This directory is named `app` so it does not shadow the installed `e2e_agent`
package when ADK puts the repo root on sys.path.
"""

import sys
from pathlib import Path

_src = Path(__file__).resolve().parents[1] / "src"
if _src.is_dir() and str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from e2e_agent.agent.agent import app, root_agent  # noqa: F401
