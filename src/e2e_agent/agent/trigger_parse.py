"""Parse chat / A2A text into a TriggerEvent.

The deployed AdkApp path does not use this: playground, GE `:streamQuery`,
and MCP all send TriggerEvent JSON. Kept for tests and a possible later
HTTP/A2A surface (see docs/geap-adk-streamquery.md).
"""

from __future__ import annotations

import json
import os
import re

from pydantic import ValidationError

from .state import TriggerEvent

USAGE = (
    "Need a GitHub repo and an Actions run id. Send one of:\n"
    '- {"trigger_source":"TEST_OPERATOR","repo":"owner/repo","run_id":"123"}\n'
    "- https://github.com/owner/repo/actions/runs/123\n"
    "- investigate owner/repo run 123"
)

_GITHUB_RUN = re.compile(
    r"https?://github\.com/(?P<repo>[^/\s]+/[^/\s]+)/actions/runs/(?P<run_id>\d+)",
    re.IGNORECASE,
)
_REPO = re.compile(r"\b(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\b")
_RUN_LABELED = re.compile(
    r"\b(?:run[_ ]?id[:\s#]*|run\s+|actions/runs/)(?P<run_id>\d{4,})\b",
    re.IGNORECASE,
)
_BARE_RUN = re.compile(r"\b(?P<run_id>\d{6,})\b")


def parse_trigger_text(
    text: str, *, default_repo: str | None = None
) -> TriggerEvent | None:
    """Return a TriggerEvent if `text` names a repo and run, else None."""
    text = (text or "").strip()
    if not text:
        return None

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return TriggerEvent.model_validate(data)
    except (json.JSONDecodeError, ValidationError, TypeError):
        pass

    github = _GITHUB_RUN.search(text)
    if github:
        return TriggerEvent(repo=github.group("repo"), run_id=github.group("run_id"))

    repo = None
    repo_match = _REPO.search(text)
    if repo_match:
        candidate = repo_match.group("repo")
        if "github.com" not in candidate.lower():
            repo = candidate

    run_id = None
    labeled = _RUN_LABELED.search(text)
    if labeled:
        run_id = labeled.group("run_id")
    else:
        bare = _BARE_RUN.search(text)
        if bare:
            run_id = bare.group("run_id")

    repo = repo or default_repo or os.environ.get("E2E_AGENT_REPO")
    if repo and run_id:
        return TriggerEvent(repo=repo, run_id=run_id)
    return None
