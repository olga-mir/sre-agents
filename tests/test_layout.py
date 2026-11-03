"""Each agent is its own deployment, and the layout keeps them that way.

Every rule here fails when one agent starts depending on, or shipping, the
other's files: that is the coupling the per-agent layout exists to remove.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
AGENTS = sorted(p for p in (ROOT / "agents").iterdir() if p.is_dir())
NAMES = [p.name for p in AGENTS]

# What every agent directory must carry to be deployed alone.
REQUIRED = [
    "Dockerfile",
    "pyproject.toml",
    "requirements.txt",
    "agents-cli-manifest.yaml",
    "deploy/deploy.conf",
    "deploy/runtime.env",
    "deploy/grant_iam.sh",
]


def test_the_two_agents_are_the_two_deployments():
    assert NAMES == ["e2e-agent", "sre-coordinator"]


@pytest.mark.parametrize("agent", AGENTS, ids=NAMES)
def test_agent_carries_everything_needed_to_deploy_alone(agent):
    missing = [f for f in REQUIRED if not (agent / f).is_file()]
    assert not missing, f"{agent.name} is missing {missing}"


@pytest.mark.parametrize("agent", AGENTS, ids=NAMES)
def test_manifest_points_at_a_package_in_the_same_directory(agent):
    manifest = yaml.safe_load((agent / "agents-cli-manifest.yaml").read_text())
    assert manifest["name"] == agent.name
    assert (agent / manifest["agent_directory"] / "__init__.py").is_file()


@pytest.mark.parametrize("agent", AGENTS, ids=NAMES)
def test_dockerfile_names_the_module_it_serves(agent):
    """agent_serving has no default AGENT_MODULE, so the image must set it,
    and the module must exist in this agent's own package."""
    dockerfile = (agent / "Dockerfile").read_text()
    match = re.search(r"^ENV AGENT_MODULE=(\S+)$", dockerfile, re.MULTILINE)
    assert match, f"{agent.name}/Dockerfile does not set AGENT_MODULE"
    module_path = agent / (match.group(1).replace(".", "/") + ".py")
    assert module_path.is_file(), module_path
    assert "agent_serving.fast_api_app:app" in dockerfile


@pytest.mark.parametrize("agent", AGENTS, ids=NAMES)
def test_deploy_conf_names_the_directory(agent):
    conf = (agent / "deploy" / "deploy.conf").read_text()
    assert re.search(rf"^NAME={re.escape(agent.name)}$", conf, re.MULTILINE)


def _imports(package: Path) -> set[str]:
    found: set[str] = set()
    for path in package.rglob("*.py"):
        for m in re.finditer(r"^\s*(?:from|import)\s+([A-Za-z_][\w]*)", path.read_text(), re.MULTILINE):
            found.add(m.group(1))
    return found


def test_agents_do_not_import_each_other():
    """The coordinator reaches e2e-agent over stream_query, not by import. An
    import would drag tree-sitter and the GitHub client into its image."""
    assert "sre_coordinator" not in _imports(ROOT / "agents/e2e-agent/e2e_agent")
    assert "e2e_agent" not in _imports(ROOT / "agents/sre-coordinator/sre_coordinator")


def test_shared_lib_does_not_import_an_agent():
    shared = _imports(ROOT / "libs/agent_serving/agent_serving")
    assert not shared & {"e2e_agent", "sre_coordinator"}


def test_coordinator_image_does_not_ship_the_e2e_stack():
    for name in ("pyproject.toml", "requirements.txt"):
        lines = (ROOT / "agents/sre-coordinator" / name).read_text().splitlines()
        text = "\n".join(line for line in lines if not line.lstrip().startswith("#"))
        for heavy in ("tree-sitter", "gcsfs", "pyjwt", "httpx"):
            assert heavy not in text.lower(), f"{name} pulls in {heavy}"


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync not installed")
@pytest.mark.parametrize("agent", AGENTS, ids=NAMES)
def test_stage_is_the_agent_plus_the_shared_lib_and_nothing_else(agent, tmp_path):
    subprocess.run(
        ["bash", str(ROOT / "deploy/stage.sh"), agent.name, str(tmp_path)],
        check=True, timeout=60,
    )
    staged = {p.relative_to(tmp_path).parts[0] for p in tmp_path.iterdir()}
    assert "agent_serving" in staged
    assert "Dockerfile" in staged and "agents-cli-manifest.yaml" in staged
    assert not staged & {"tests", "deploy", ".env", "deployment_metadata.json"}
    other = {"e2e-agent": "sre_coordinator", "sre-coordinator": "e2e_agent"}[agent.name]
    assert other not in staged
