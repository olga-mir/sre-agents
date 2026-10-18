"""The code-comprehension tool layer.

Four moves: locate -> fetch once -> outline -> read a span
(docs/source-code-comprehension.md §3). Only the last two put
source in context, and both are bounded.

Every tool is a pure, sha-pinned read: it may write bytes to the store, it may
not write session state, and it holds no process-local cache.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from ..code.go_outline import outline_go_source, outline_unparsed
from ..code.model import FileOutline, Symbol
from ..github.client import GitHub, GitHubNotFound, Hunk
from ..store import SourceStore, evidence_filename

WINDOW_CAP = 120  # hard cap on read_window, in lines
SYMBOL_CAP = 200  # a span longer than this is truncated, and says so
GO_SUFFIXES = (".go",)
RAW_DIFF_PATHS = ("go.mod", "go.sum")  # small and structural; never outlined
MAX_CONCURRENT_FETCHES = 6  # cap on in-flight GitHub calls from one batch

UNTRUSTED_OPEN, UNTRUSTED_CLOSE = "<untrusted_ci_output>", "</untrusted_ci_output>"


def _envelope(text: str) -> str:
    """All remote text is data authored by the change under investigation."""
    return f"{UNTRUSTED_OPEN}\n{text}\n{UNTRUSTED_CLOSE}"


def _slice(source: bytes, start: int, end: int) -> str:
    lines = source.decode("utf-8", "replace").splitlines()
    start = max(1, start)
    end = min(len(lines), end)
    return "\n".join(f"{n:>6}  {lines[n - 1]}" for n in range(start, end + 1))


def resolve_symbol(outline: FileOutline, query: str) -> Optional[Symbol]:
    """Match a symbol the way failure output addresses it.

    Accepts `TestReconcile`, `TestReconcile/missing_backend`, `(*Server).handleAuth`
    and a bare method name. Subtest names are already normalised the way Go
    prints them, so a name lifted verbatim out of `--- FAIL:` matches.
    """
    query = query.strip()
    if "/" in query:
        parent_name, _, child_name = query.partition("/")
        parent = resolve_symbol(outline, parent_name)
        if parent is None:
            return None
        for child in parent.children:
            if child.name == child_name:
                return child
        return parent  # finest address available: the whole test function
    for sym in outline.symbols:
        if sym.name == query:
            return sym
    # bare method name, e.g. "handleAuth" for "(*Server).handleAuth"
    matches = [s for s in outline.symbols
               if s.kind == "method" and s.name.rsplit(".", 1)[-1] == query]
    return matches[0] if len(matches) == 1 else None


def symbol_containing(outline: FileOutline, line: int) -> Optional[Symbol]:
    """The narrowest symbol spanning a line - a whole semantic unit, not a
    window straddling two functions."""
    best: Optional[Symbol] = None
    for sym in outline.flat():
        if sym.start_line <= line <= sym.end_line:
            if best is None or (sym.end_line - sym.start_line) < (best.end_line - best.start_line):
                best = sym
    return best


_MISSING_PATH_HINT = (
    "This path is not in the git tree at this sha. That is a result, not a "
    "prompt to guess another filename. Do not invent sibling paths for a "
    "package the build log says cannot be found. Retry the other pinned sha "
    "only when list_changed_files shows this path was renamed or deleted."
)


class CodeTools:
    """Bound to one repo and one run. `run_id` only names the artifact prefix."""

    def __init__(self, gh: GitHub, store: SourceStore, run_id: str, head_repo: Optional[str] = None):
        self.gh = gh
        self.store = store
        self.run_id = run_id
        self.head_repo = head_repo
        # Bounds how many of a batch's GitHub calls are in flight at once, so
        # e.g. outline_files(20 targets) fans out politely instead of opening
        # 20 simultaneous connections to the GitHub API.
        self._fetch_semaphore = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)

    # ---- fetch-once ------------------------------------------------------

    def _key(self, path: str, sha: str) -> str:
        return evidence_filename(
            run_id=self.run_id, kind="source_file", pinned_to=sha, key=path
        )

    async def _fetch(self, path: str, sha: str, repo: Optional[str]) -> bytes:
        key = self._key(path, sha)
        cached = await self.store.get(key)
        if cached is not None:
            return cached
        # Only the network call is gated: a cache hit costs nothing and
        # should never queue behind other in-flight fetches.
        async with self._fetch_semaphore:
            data = await self.gh.get_file(path, sha, repo=repo)
        await self.store.put(key, data)
        return data

    async def _source(self, path: str, sha: str) -> bytes:
        try:
            return await self._fetch(path, sha, None)
        except GitHubNotFound:
            if self.head_repo and self.head_repo != self.gh.repo:
                return await self._fetch(path, sha, self.head_repo)
            raise

    async def _outline(self, path: str, sha: str) -> FileOutline:
        source = await self._source(path, sha)
        if path.endswith(GO_SUFFIXES):
            return outline_go_source(source, path, sha)
        return outline_unparsed(
            source, path, sha, reason=f"no grammar for {path.rsplit('.', 1)[-1]!r}; "
            "use read_window on the sampled ranges"
        )

    # ---- tools -----------------------------------------------------------

    async def list_changed_files(self, base_sha: str, head_sha: str) -> dict[str, Any]:
        """Paths changed between two SHAS, with per-file hunk line ranges. No content.

        This is the manifest to use: it is pinned to the sha the run built, so
        its hunk line numbers line up with the outline of that same sha. A PR's
        own file list is served at the PR's CURRENT head, which has usually
        moved on. Works on push runs too, which have no PR.

        `hunks` are head-side line ranges; feed them to changed_symbols rather
        than reading the diff text.
        """
        files = await self.gh.compare_files(base_sha, head_sha)
        return {
            "base_sha": base_sha,
            "head_sha": head_sha,
            "file_count": len(files),
            "files": [
                {
                    "path": f.path,
                    "status": f.status,
                    "additions": f.additions,
                    "deletions": f.deletions,
                    "hunks": [[h.start, h.end] for h in f.hunks],
                    "previous_path": f.previous_path,
                    "raw_diff_recommended": f.path.endswith(RAW_DIFF_PATHS),
                }
                for f in files
            ],
        }

    async def list_pr_files(self, pr_number: int) -> dict[str, Any]:
        """PR-level view: title, author, and the changed-file manifest AT THE
        PR'S CURRENT HEAD. Not sha-pinned - do not join its hunks against an
        outline; use list_changed_files for that."""
        pr = await self.gh.get_pr(pr_number)
        files = await self.gh.pr_files(pr_number)
        return {
            "pr_number": pr_number,
            "title": pr.get("title"),
            "author": (pr.get("user") or {}).get("login"),
            "head_sha": (pr.get("head") or {}).get("sha"),
            "pinned": False,
            "file_count": len(files),
            "files": [{"path": f.path, "status": f.status,
                       "additions": f.additions, "deletions": f.deletions}
                      for f in files],
        }

    async def outline_file(self, path: str, sha: str) -> dict[str, Any]:
        """Fetch one file at one commit, store it, and return its structure only.

        NEVER returns file contents. A 3 000-line file yields ~60 lines of
        outline. Use a returned symbol name with read_symbol, or a line number
        from a build error or stack frame with symbol_at_line.

        Symbol names match how Go test/build output addresses them - 'TestX'
        from '--- FAIL: TestX', '(*T).Method' from a panic frame - so failure
        text can be looked up directly. Subtest names are normalised the way Go
        prints them (spaces become underscores).

        `sha` must be a commit sha, never a branch name: a PR is a moving target
        and a re-triage of the same run must reach the same bytes.

        Returns generated=True for files carrying the 'Code generated ... DO NOT
        EDIT.' marker - a failure there points at the generator, not the file.

        A missing path at this sha returns error=file_not_found instead of
        raising. Absence is the result: do not invent another path, and do not
        retry the other sha unless the change renamed this file.
        """
        try:
            outline = await self._outline(path, sha)
        except GitHubNotFound as exc:
            return {
                "error": "file_not_found",
                "path": path,
                "sha": sha,
                "repo": exc.repo,
                "hint": _MISSING_PATH_HINT,
            }
        return {
            "path": path,
            "sha": sha,
            "total_lines": outline.total_lines,
            "rung": outline.rung,
            "generated": outline.generated,
            "symbol_count": len(outline.flat()),
            "outline": outline.render(),
        }

    async def read_symbol(self, path: str, sha: str, symbol: str) -> dict[str, Any]:
        """The exact span of one named symbol, from bytes already fetched.

        Address by name, not by line: line numbers shift between base and head,
        symbol names do not, so the same call works against both refs.
        Truncates at 200 lines and says so.
        """
        outline = await self._outline(path, sha)
        sym = resolve_symbol(outline, symbol)
        if sym is None:
            return {
                "error": f"no symbol {symbol!r} in {path}",
                "available": [s.name for s in outline.symbols][:60],
            }
        source = await self._source(path, sha)
        end = min(sym.end_line, sym.start_line + SYMBOL_CAP - 1)
        return {
            "path": path,
            "sha": sha,
            "symbol": sym.name,
            "kind": sym.kind,
            "start_line": sym.start_line,
            "end_line": sym.end_line,
            "truncated": end < sym.end_line,
            "heuristic_name": sym.heuristic,
            "text": _envelope(_slice(source, sym.start_line, end)),
        }

    async def symbol_at_line(self, path: str, sha: str, line: int) -> dict[str, Any]:
        """The symbol CONTAINING a line, then its span.

        This is the tool for `route.go:120:5` - build errors, t.Errorf locations
        and panic frames all hand you a line. Returns the whole semantic unit
        rather than a window straddling two functions.
        """
        outline = await self._outline(path, sha)
        sym = symbol_containing(outline, line)
        if sym is None:
            return {
                "path": path, "sha": sha, "line": line,
                "note": "line is not inside any symbol (package decl, blank, or "
                        "top-level comment); use read_window",
            }
        result = await self.read_symbol(path, sha, sym.name)
        result["matched_line"] = line
        return result

    # ---- batched tools -----------------------------------------------------
    # Each wraps its singular counterpart in asyncio.gather instead of a
    # for-loop: N targets in one round trip, fetched concurrently (capped by
    # _fetch_semaphore), instead of one round trip per target.

    async def outline_files(self, targets: list[dict[str, str]]) -> dict[str, Any]:
        """Batched outline_file: outline several files at their pinned shas
        in one round trip instead of one call per file. Each target is
        {"path": ..., "sha": ...}.
        """
        results = await asyncio.gather(
            *(self.outline_file(t["path"], t["sha"]) for t in targets)
        )
        return {"results": list(results)}

    async def read_symbols(self, targets: list[dict[str, str]]) -> dict[str, Any]:
        """Batched read_symbol: read several named symbols - from the same
        file or different files - in one round trip instead of one call per
        symbol. Each target is {"path": ..., "sha": ..., "symbol": ...}.
        """
        results = await asyncio.gather(
            *(self.read_symbol(t["path"], t["sha"], t["symbol"]) for t in targets)
        )
        return {"results": list(results)}

    async def read_symbols_at_lines(self, targets: list[dict[str, Any]]) -> dict[str, Any]:
        """Batched symbol_at_line: resolve several file:line addresses to
        their enclosing symbols in one round trip instead of one call per
        address. Each target is {"path": ..., "sha": ..., "line": ...}.
        """
        results = await asyncio.gather(
            *(self.symbol_at_line(t["path"], t["sha"], t["line"]) for t in targets)
        )
        return {"results": list(results)}

    async def read_window(self, path: str, sha: str, start: int, end: int) -> dict[str, Any]:
        """Escape hatch for non-symbol regions (imports, consts, generated blobs).

        Hard cap 120 lines; a wider request is truncated, not refused.
        """
        try:
            source = await self._source(path, sha)
        except GitHubNotFound as exc:
            return {
                "error": "file_not_found",
                "path": path,
                "sha": sha,
                "repo": exc.repo,
                "hint": _MISSING_PATH_HINT,
            }
        capped_end = min(end, start + WINDOW_CAP - 1)
        return {
            "path": path,
            "sha": sha,
            "start_line": start,
            "end_line": capped_end,
            "truncated": capped_end < end,
            "text": _envelope(_slice(source, start, capped_end)),
        }

    async def changed_symbols(self, path: str, base_sha: str,
                              head_sha: str) -> dict[str, Any]:
        """What this change did to this file, in units a human reasons about.

        A pure join of the pinned hunk line ranges against the file's symbol
        spans - no diff text and no extra fetch beyond the outline. Prefer this
        over reading 400 lines of unified diff. For go.mod / go.sum read the raw
        hunk instead: a dependency bump is already small and structural.
        """
        files = await self.gh.compare_files(base_sha, head_sha)
        match = next((f for f in files if f.path == path), None)
        if match is None:
            return {"error": f"{path} is unchanged between "
                             f"{base_sha[:12]} and {head_sha[:12]}"}
        sha = head_sha
        if path.endswith(RAW_DIFF_PATHS):
            return {
                "path": path, "sha": sha,
                "note": "read the raw hunk from list_changed_files",
                "hunks": [[h.start, h.end] for h in match.hunks],
            }
        outline = await self._outline(path, sha)
        touched: list[dict[str, Any]] = []
        for sym in outline.symbols:
            hit = [h for h in match.hunks
                   if h.start <= sym.end_line and h.end >= sym.start_line]
            if hit:
                touched.append({
                    "kind": sym.kind,
                    "name": sym.name,
                    "start_line": sym.start_line,
                    "end_line": sym.end_line,
                    "changed_lines": [[h.start, h.end] for h in hit],
                })
        outside = _hunks_outside(match.hunks, outline.symbols)
        return {
            "path": path,
            "sha": sha,
            "status": match.status,
            "generated": outline.generated,
            "changed_symbols": touched,
            "changed_lines_outside_symbols": [[h.start, h.end] for h in outside],
        }

    async def blame_lines(self, path: str, sha: str, start: int, end: int) -> dict[str, Any]:
        """Which commit last touched these lines. GraphQL blame - no clone."""
        ranges = await self.gh.blame(path, sha, start, end)
        return {"path": path, "sha": sha, "start_line": start, "end_line": end,
                "ranges": ranges}


def _hunks_outside(hunks: list[Hunk], symbols: list[Symbol]) -> list[Hunk]:
    """Changed lines that land in no symbol - imports, package-level edits."""
    out = []
    for h in hunks:
        if not any(s.start_line <= h.end and s.end_line >= h.start for s in symbols):
            out.append(h)
    return out
