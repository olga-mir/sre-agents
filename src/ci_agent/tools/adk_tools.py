"""ADK-facing tool functions for investigator_loop / targeted_deep_dive.

Each function is a thin binding: it reads the pinned coordinates from session
state, never writes state, and delegates to CodeTools. The docstrings are the
model's function specification, so they carry the operational bounds.
"""

from __future__ import annotations

from typing import Any

from ..github.client import GitHub
from ..run_context import RunContext
from .artifact_store import ArtifactStore
from .code_tools import CodeTools
from .log_tools import LogTools


def _run_context(tool_context: Any) -> RunContext:
    raw = tool_context.state.get("run_context")
    if not raw:
        raise RuntimeError("run_context is not pinned; pin_run_context must run first")
    return RunContext(**raw)


async def _tools(tool_context: Any) -> tuple[GitHub, CodeTools, RunContext]:
    rc = _run_context(tool_context)
    gh = GitHub(rc.repo)
    return gh, CodeTools(gh, ArtifactStore(tool_context), run_id=rc.run_id), rc


async def _log(tool_context: Any) -> tuple[GitHub, LogTools, RunContext]:
    rc = _run_context(tool_context)
    gh = GitHub(rc.repo)
    return gh, LogTools(gh, ArtifactStore(tool_context), run_id=rc.run_id,
                        module=rc.module), rc


async def list_failed_jobs(tool_context: Any) -> dict[str, Any]:
    """List this run's failed, timed-out and cancelled jobs, with their failed
    step names. The user brief already has each job_id; skip this unless you
    need failed_steps that were not in the brief."""
    rc = _run_context(tool_context)
    return {
        "repo": rc.repo, "run_id": rc.run_id,
        "head_sha": rc.head_sha, "base_sha": rc.base_sha,
        "pr_number": rc.pr_number,
        "jobs": [j.model_dump() for j in rc.failed_jobs],
    }


async def list_failure_addresses(job_id: int, tool_context: Any) -> dict[str, Any]:
    """List every address one job's log names: failing tests and subtests, the
    file:line of each assertion, and panic frames.

    START HERE for a job. Call it in the SAME turn as list_changed_files and
    find_in_log - they do not depend on this result. Each address feeds
    outline_file plus read_symbol or symbol_at_line. Do not search the source
    for the rendered error text - Go assembles most messages at runtime with
    fmt.Errorf, so the string in the log does not exist in the source.
    """
    gh, tools, _ = await _log(tool_context)
    async with gh:
        return await tools.list_failure_addresses(job_id)


async def find_in_log(job_id: int, pattern: str, tool_context: Any) -> dict[str, Any]:
    """Regex-search one job's log. Returns matching lines with line numbers,
    never the log. Caps at 60 hits and reports truncation.

    Independent of list_failure_addresses and list_changed_files: emit it in
    the same turn, using job_id and a pattern from the brief (focused test
    name or failed step)."""
    gh, tools, _ = await _log(tool_context)
    async with gh:
        return await tools.find_in_log(job_id, pattern)


async def read_log_window(job_id: int, start: int, end: int,
                          tool_context: Any) -> dict[str, Any]:
    """Read a bounded slice of one job's log around a line number found by
    find_in_log or list_failure_addresses. Hard cap 120 lines."""
    gh, tools, _ = await _log(tool_context)
    async with gh:
        return await tools.read_log_window(job_id, start, end)


async def get_job_log_summary(job_id: int, tool_context: Any) -> dict[str, Any]:
    """Read the tail of one job's log. Use this when a job failed with no test
    assertion at all - OOM kill, evicted runner, image pull failure, timeout."""
    gh, tools, _ = await _log(tool_context)
    async with gh:
        return await tools.get_job_log_summary(job_id)


async def outline_file(path: str, sha: str, tool_context: Any) -> dict[str, Any]:
    """Fetch one file at one commit, store it, and return its structure only.

    NEVER returns file contents. A 3 000-line file yields ~60 lines of outline.
    Use a returned symbol name with read_symbol, or a line number from a build
    error or stack frame with symbol_at_line.

    Symbol names match how Go test/build output addresses them - 'TestX' from
    '--- FAIL: TestX', '(*T).Method' from a panic frame - so failure text can be
    looked up directly. Subtest names are normalised the way Go prints them
    (spaces become underscores).

    `sha` must be a commit sha, never a branch name: a PR is a moving target and
    a re-triage of the same run must reach the same bytes.

    Returns generated=True for files carrying the 'Code generated ... DO NOT
    EDIT.' marker - a failure there points at the generator, not the file.
    """
    gh, tools, _ = await _tools(tool_context)
    async with gh:
        return await tools.outline_file(path, sha)


async def read_symbol(path: str, sha: str, symbol: str,
                      tool_context: Any) -> dict[str, Any]:
    """Read the exact span of one named symbol from a file already outlined.

    Address by name, not by line: line numbers shift between base and head,
    symbol names do not. Accepts 'TestReconcile', 'TestReconcile/missing_backend'
    and '(*Server).handleAuth'. Truncates at 200 lines and reports truncated=true.
    """
    gh, tools, _ = await _tools(tool_context)
    async with gh:
        return await tools.read_symbol(path, sha, symbol)


async def symbol_at_line(path: str, sha: str, line: int,
                         tool_context: Any) -> dict[str, Any]:
    """Read the symbol CONTAINING a line - use this for 'route.go:120:5'.

    Build errors, t.Errorf locations and panic frames all hand you a line.
    Returns the whole enclosing function or method rather than a window that
    straddles two of them.
    """
    gh, tools, _ = await _tools(tool_context)
    async with gh:
        return await tools.symbol_at_line(path, sha, line)


async def read_window(path: str, sha: str, start: int, end: int,
                      tool_context: Any) -> dict[str, Any]:
    """Read a line range - the escape hatch for regions that are not symbols
    (imports, package-level declarations, generated blobs). Hard cap 120 lines.
    Prefer read_symbol or symbol_at_line whenever an address is available.
    """
    gh, tools, _ = await _tools(tool_context)
    async with gh:
        return await tools.read_window(path, sha, start, end)


async def outline_files(targets: list[dict[str, str]],
                        tool_context: Any) -> dict[str, Any]:
    """Batched outline_file: outline several files at their pinned shas in
    ONE round trip instead of one call per file, fetched concurrently.

    Use this instead of repeated outline_file calls whenever you already have
    every path in hand - e.g. every distinct file in list_failure_addresses'
    file_lines, or every path list_changed_files returned. Each target:
    {"path": ..., "sha": ...}.
    """
    gh, tools, _ = await _tools(tool_context)
    async with gh:
        return await tools.outline_files(targets)


async def read_symbols(targets: list[dict[str, str]],
                       tool_context: Any) -> dict[str, Any]:
    """Batched read_symbol: read several named symbols in ONE round trip
    instead of one call per symbol, fetched concurrently. Each target:
    {"path": ..., "sha": ..., "symbol": ...}.
    """
    gh, tools, _ = await _tools(tool_context)
    async with gh:
        return await tools.read_symbols(targets)


async def read_symbols_at_lines(targets: list[dict[str, Any]],
                                tool_context: Any) -> dict[str, Any]:
    """Batched symbol_at_line: resolve several file:line addresses to their
    enclosing symbols in ONE round trip instead of one call per address,
    fetched concurrently - e.g. every entry in list_failure_addresses'
    file_lines. Each target: {"path": ..., "sha": ..., "line": ...}.
    """
    gh, tools, _ = await _tools(tool_context)
    async with gh:
        return await tools.read_symbols_at_lines(targets)


async def list_changed_files(tool_context: Any) -> dict[str, Any]:
    """List the files this run's change touched, with hunk line ranges, no content.

    Pinned to the run's base_sha...head_sha, so the line ranges line up with the
    outline of the same sha. Works on push runs, which have no PR at all.
    Takes no arguments and does not depend on log tools: emit it in the same
    turn as list_failure_addresses and find_in_log.
    """
    gh, tools, rc = await _tools(tool_context)
    async with gh:
        return await tools.list_changed_files(rc.base_sha, rc.head_sha)


async def changed_symbols(path: str, tool_context: Any) -> dict[str, Any]:
    """Report what the change did to one file, as named symbols rather than diff text.

    Answers 'what did this change touch, in units a human reasons about'. Prefer
    it over reading a unified diff. For go.mod / go.sum it points at the raw hunk
    instead: a dependency bump is already small and structural.
    """
    gh, tools, rc = await _tools(tool_context)
    async with gh:
        return await tools.changed_symbols(path, rc.base_sha, rc.head_sha)


async def blame_lines(path: str, start: int, end: int,
                      tool_context: Any) -> dict[str, Any]:
    """Report which commit last touched each of these lines, at the run's head sha.

    This is the no-clone replacement for a commit walk. Use it after
    changed_symbols has narrowed the blame to a span.
    """
    gh, tools, rc = await _tools(tool_context)
    async with gh:
        return await tools.blame_lines(path, rc.head_sha, start, end)


LOG_TOOLS = [
    list_failed_jobs,
    list_failure_addresses,
    find_in_log,
    read_log_window,
    get_job_log_summary,
]

CODE_TOOLS = [
    list_changed_files,
    outline_file,
    outline_files,
    symbol_at_line,
    read_symbol,
    read_symbols,
    read_symbols_at_lines,
    changed_symbols,
    blame_lines,
    read_window,
]
"""Registration order is the order the model should reach for them."""

INVESTIGATOR_TOOLS = LOG_TOOLS + CODE_TOOLS

PR_SCOPED_TOOLS = frozenset({"list_changed_files", "changed_symbols"})
"""Tools that need a diff. A frozenset, not an inline string literal: renaming a
tool must break a name lookup rather than silently turning the guard into a
no-op, which is how the shipped guard rotted."""
