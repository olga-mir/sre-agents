"""Mechanical bound checks over src/ and deploy/.

Why these are tests and not a review checklist: the defects they look for are
*absences* - a `timeout=` that was never typed, a counter that is incremented
and never compared. An AST walk cannot miss an absence.

Each rule has two tests: one that runs it over the real tree, and one that runs
it over an inline snippet, so the rule is proven to have teeth even when the
tree is clean.

ACCEPTED is a ratchet in both directions. A new unbounded call fails until it
is bounded or accepted with a reason. An accepted entry that no longer matches
also fails, so the list cannot rot into a list of things that used to be true.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "e2e_agent"
DEPLOY = ROOT / "deploy"


# --- deliberate exceptions, each with the reason it is one -------------------

ACCEPTED: dict[str, str] = {
    "while_true:src/e2e_agent/github/client.py:_paged":
        "GitHub pagination. Each page is one bounded HTTP call, but the page "
        "count is not capped. Capping it means truncating results, which is a "
        "product decision, not a bound.",
    "uncancellable_thread:src/e2e_agent/agent/nodes.py:record_outcome":
        "The BigQuery client is sync. Workflow cancellation does not reach this "
        "thread, so every wait inside it carries its own deadline "
        "(E2E_AGENT_BQ_TIMEOUT_S) instead of relying on an outer timeout.",
}

# Budget keys that exist in state. A key here must appear in a comparison
# somewhere in src/, or be declared below with the reason it does not.
BUDGET_KEYS = {
    "current_turn", "max_turns", "token_limit", "tokens_used",
    "dormancy_retry_count",
}

UNCOMPARED_BUDGET_KEYS: dict[str, str] = {
    "token_limit": "hardcoded 0 and never read as a limit; context is "
                   "uncompacted. Known gap.",
    "tokens_used": "never written, so the limit it pairs with cannot fire. "
                   "Known gap.",
    "dormancy_retry_count": "incremented on malformed engineer feedback and "
                            "never compared, so dormancy_gate can re-prompt "
                            "without end. Known gap.",
}

# Env knobs that set a bound. A bound whose production value is whatever the
# code default happens to be is not a bound anyone chose, so each of these is
# pinned explicitly in the deployed config.
BOUND_KNOBS = {
    "E2E_AGENT_MAX_TURNS",
    "ADK_MAX_LLM_CALLS",
    "E2E_AGENT_BQ_TIMEOUT_S",
    "E2E_AGENT_MODEL_RETRY_ATTEMPTS",
}


# --- rules ------------------------------------------------------------------

# Methods whose zero-argument form is a wait with no deadline. `join`/`wait`
# with arguments is str.join or something else; the zero-arg form is not.
BLOCKING_CALLS = {"result", "cancel", "join", "wait", "acquire"}
HTTP_VERBS = {"get", "post", "put", "patch", "delete", "head", "request", "stream"}
HTTP_CLIENTS = {"Client", "AsyncClient"}


def _called_name(call: ast.Call) -> str | None:
    """The bare name of what is being called.

    Both forms matter. `job.cancel()` is an Attribute; `cancel = getattr(job,
    "cancel", None); cancel()` is a Name, and that is the form the first
    attempt at this check missed - the one that was live in writer.py.
    """
    f = call.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return None


def _has_kw(call: ast.Call, name: str) -> bool:
    return any(k.arg == name for k in call.keywords)


def _enclosing(tree: ast.AST) -> dict[int, str]:
    """lineno -> enclosing def name, so an ACCEPTED key survives line shifts."""
    spans: dict[int, str] = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for line in range(n.lineno, (n.end_lineno or n.lineno) + 1):
                spans.setdefault(line, n.name)
    return spans


def find_unbounded(tree: ast.AST, relpath: str) -> dict[str, str]:
    """rule:path:func -> detail, for every wait or loop with no bound."""
    where = _enclosing(tree)
    out: dict[str, str] = {}

    def add(rule: str, node: ast.AST, detail: str) -> None:
        func = where.get(node.lineno, "<module>")
        out[f"{rule}:{relpath}:{func}"] = f"line {node.lineno}: {detail}"

    for n in ast.walk(tree):
        if isinstance(n, ast.While) and isinstance(n.test, ast.Constant) \
                and n.test.value is True:
            add("while_true", n, "while True with no iteration cap")
            continue
        if not isinstance(n, ast.Call):
            continue
        name = _called_name(n)
        if name in BLOCKING_CALLS and not n.args and not _has_kw(n, "timeout"):
            add("unbounded_wait", n,
                f"{name}() with no timeout= - check the library default before "
                "assuming it has one")
        f = n.func
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                and f.value.id in {"httpx", "requests"}:
            if f.attr in HTTP_VERBS and not _has_kw(n, "timeout"):
                add("unbounded_http", n, f"{f.value.id}.{f.attr}() with no timeout=")
            if f.attr in HTTP_CLIENTS and not _has_kw(n, "timeout"):
                add("unbounded_http", n,
                    f"{f.value.id}.{f.attr}() with no timeout= - the default is "
                    "no timeout at all")
        if isinstance(f, ast.Attribute) and f.attr == "to_thread":
            add("uncancellable_thread", n,
                "asyncio.to_thread cannot be cancelled from outside; the work "
                "inside needs its own deadline")
    return out


def compared_keys(tree: ast.AST) -> set[str]:
    """String keys that appear inside a comparison - i.e. actually used as a bound."""
    found: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Compare):
            for sub in ast.walk(n):
                if isinstance(sub, ast.Constant) and sub.value in BUDGET_KEYS:
                    found.add(sub.value)
    return found


def deployed_env_vars() -> set[str]:
    """Keys of the env_vars dict literal in deploy/deploy_agent.py."""
    tree = ast.parse((DEPLOY / "deploy_agent.py").read_text())
    for n in ast.walk(tree):
        if not isinstance(n, ast.Dict):
            continue
        for key, value in zip(n.keys, n.values):
            if isinstance(key, ast.Constant) and key.value == "env_vars" \
                    and isinstance(value, ast.Dict):
                return {k.value for k in value.keys
                        if isinstance(k, ast.Constant)}
    return set()


def _tree_findings() -> dict[str, str]:
    files = sorted(SRC.rglob("*.py")) + sorted(DEPLOY.glob("*.py"))
    found: dict[str, str] = {}
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        found.update(find_unbounded(ast.parse(path.read_text()), rel))
    return found


# --- the tree ---------------------------------------------------------------

def test_every_wait_and_loop_is_bounded_or_declared():
    found = _tree_findings()
    undeclared = {k: v for k, v in found.items() if k not in ACCEPTED}
    assert not undeclared, (
        "unbounded wait or loop with no declared reason:\n  "
        + "\n  ".join(f"{k} ({v})" for k, v in sorted(undeclared.items()))
        + "\n\nBound it, or add it to ACCEPTED in this file with the reason."
    )


def test_no_stale_exceptions():
    found = _tree_findings()
    stale = sorted(set(ACCEPTED) - set(found))
    assert not stale, (
        f"ACCEPTED entries that no longer match anything: {stale}. "
        "If the call was bounded, delete the entry."
    )


def test_budget_counters_are_compared_or_declared():
    compared: set[str] = set()
    for path in sorted(SRC.rglob("*.py")):
        compared |= compared_keys(ast.parse(path.read_text()))
    uncompared = BUDGET_KEYS - compared
    assert uncompared == set(UNCOMPARED_BUDGET_KEYS), (
        "a counter that is written and never compared is not a bound.\n"
        f"  never compared: {sorted(uncompared)}\n"
        f"  declared as known gaps: {sorted(UNCOMPARED_BUDGET_KEYS)}\n"
        "Compare it against a limit, or declare it in UNCOMPARED_BUDGET_KEYS."
    )


def test_bound_knobs_are_pinned_in_the_deployed_config():
    missing = sorted(BOUND_KNOBS - deployed_env_vars())
    assert not missing, (
        f"bound knobs not pinned in deploy_agent.py env_vars: {missing}. "
        "In production the bound would be whatever the code default happens "
        "to be, which is not a number anyone chose."
    )


# --- the rules themselves (teeth, independent of the tree) ------------------

def _check(source: str) -> dict[str, str]:
    return find_unbounded(ast.parse(source), "x.py")


def test_rule_catches_result_with_no_timeout():
    bad = _check("def f(job):\n    job.result()\n")
    assert "unbounded_wait:x.py:f" in bad
    assert not _check("def f(job):\n    job.result(timeout=30)\n")


def test_rule_catches_a_blocking_call_hidden_behind_a_local_name():
    """The form that was live in writer.py: getattr, then call the local."""
    src = ('def f(job):\n'
           '    cancel = getattr(job, "cancel", None)\n'
           '    cancel()\n')
    assert "unbounded_wait:x.py:f" in _check(src)
    ok = ('def f(job):\n'
          '    cancel = getattr(job, "cancel", None)\n'
          '    cancel(retry=None, timeout=5)\n')
    assert not _check(ok)


def test_rule_does_not_flag_str_join():
    assert not _check('def f(xs):\n    return ", ".join(xs)\n')


def test_rule_catches_http_calls_and_clients_with_no_timeout():
    assert "unbounded_http:x.py:f" in _check(
        'import httpx\ndef f():\n    httpx.post("/x", json={})\n')
    assert "unbounded_http:x.py:f" in _check(
        'import httpx\ndef f():\n    httpx.AsyncClient(headers={})\n')
    assert not _check(
        'import httpx\ndef f():\n    httpx.post("/x", timeout=10)\n')


def test_rule_catches_while_true_and_to_thread():
    assert "while_true:x.py:f" in _check("def f():\n    while True:\n        pass\n")
    assert "uncancellable_thread:x.py:f" in _check(
        "import asyncio\nasync def f(w):\n    await asyncio.to_thread(w)\n")
