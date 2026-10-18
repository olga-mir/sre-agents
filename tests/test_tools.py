import asyncio

import pytest

from e2e_agent.github.client import ChangedFile, Hunk, parse_hunks
from e2e_agent.store import MemoryStore, evidence_candidates, evidence_filename
from e2e_agent.tools import code_tools
from e2e_agent.tools.code_tools import (
    UNTRUSTED_CLOSE, UNTRUSTED_OPEN, WINDOW_CAP, CodeTools,
)

SRC = b'''package proxy

func alpha() int {
\treturn 1
}

func beta() int {
\treturn 2
}
''' + b"// filler\n" * 200

HEAD = "a" * 40
BASE = "b" * 40


class FakeGitHub:
    """Counts fetches so 'fetch once' is a tested property, not a comment."""

    repo = "o/r"

    def __init__(self, files: dict[str, bytes], changed: list[ChangedFile]):
        self.files = files
        self.changed = changed
        self.get_file_calls = 0

    async def get_file(self, path: str, sha: str, repo: str | None = None) -> bytes:
        self.get_file_calls += 1
        return self.files[path]

    async def compare_files(self, base_sha: str, head_sha: str):
        return self.changed

    async def blame(self, path, sha, start, end):
        return [{"start_line": start, "end_line": end, "sha": "c" * 40,
                 "headline": "touch it", "author": "someone",
                 "committed_at": "2026-01-01T00:00:00Z"}]


def tools(changed=None):
    gh = FakeGitHub({"pkg/proxy/route.go": SRC}, changed or [])
    return gh, CodeTools(gh, MemoryStore(), run_id="42")


async def test_outline_never_returns_contents_and_fetches_once():
    gh, t = tools()
    first = await t.outline_file("pkg/proxy/route.go", HEAD)
    await t.read_symbol("pkg/proxy/route.go", HEAD, "alpha")
    assert "return 1" not in first["outline"]
    assert gh.get_file_calls == 1     # store, not a process-local cache


async def test_read_symbol_wraps_source_in_the_untrusted_envelope():
    _, t = tools()
    got = await t.read_symbol("pkg/proxy/route.go", HEAD, "alpha")
    assert got["text"].startswith(UNTRUSTED_OPEN)
    assert got["text"].rstrip().endswith(UNTRUSTED_CLOSE)
    assert "return 1" in got["text"] and "return 2" not in got["text"]


async def test_unknown_symbol_lists_what_is_available():
    _, t = tools()
    got = await t.read_symbol("pkg/proxy/route.go", HEAD, "gamma")
    assert "error" in got and "alpha" in got["available"]


async def test_symbol_at_line_returns_the_whole_unit():
    _, t = tools()
    got = await t.symbol_at_line("pkg/proxy/route.go", HEAD, 4)
    assert got["symbol"] == "alpha"
    assert got["start_line"] == 3 and got["matched_line"] == 4


async def test_outline_files_batches_and_still_fetches_each_target_once():
    gh, t = tools()
    got = await t.outline_files([
        {"path": "pkg/proxy/route.go", "sha": HEAD},
        {"path": "pkg/proxy/route.go", "sha": HEAD},  # duplicate target
    ])
    assert len(got["results"]) == 2
    assert all("return 1" not in r["outline"] for r in got["results"])
    assert gh.get_file_calls == 1   # second hits the store cache, not GitHub


async def test_read_symbols_batches_multiple_targets():
    _, t = tools()
    got = await t.read_symbols([
        {"path": "pkg/proxy/route.go", "sha": HEAD, "symbol": "alpha"},
        {"path": "pkg/proxy/route.go", "sha": HEAD, "symbol": "beta"},
    ])
    assert [r["symbol"] for r in got["results"]] == ["alpha", "beta"]


async def test_read_symbols_at_lines_batches_multiple_addresses():
    _, t = tools()
    got = await t.read_symbols_at_lines([
        {"path": "pkg/proxy/route.go", "sha": HEAD, "line": 4},
        {"path": "pkg/proxy/route.go", "sha": HEAD, "line": 8},
    ])
    assert [r["symbol"] for r in got["results"]] == ["alpha", "beta"]


async def test_batched_fetches_run_concurrently_within_the_semaphore_cap():
    """The whole point of batching: N targets cost one wall-clock round trip,
    not N sequential ones - and a batch larger than the cap still gets bounded
    concurrency rather than hammering GitHub with everything at once."""
    files = {f"pkg/f{i}.go": SRC for i in range(code_tools.MAX_CONCURRENT_FETCHES + 2)}

    class SlowFakeGitHub(FakeGitHub):
        def __init__(self, files):
            super().__init__(files, [])
            self.max_concurrent = 0
            self._in_flight = 0

        async def get_file(self, path, sha, repo=None):
            self._in_flight += 1
            self.max_concurrent = max(self.max_concurrent, self._in_flight)
            await asyncio.sleep(0.01)
            self._in_flight -= 1
            return await super().get_file(path, sha, repo)

    gh = SlowFakeGitHub(files)
    t = CodeTools(gh, MemoryStore(), run_id="42")
    got = await t.outline_files([{"path": p, "sha": HEAD} for p in files])
    assert len(got["results"]) == len(files)
    assert 1 < gh.max_concurrent <= code_tools.MAX_CONCURRENT_FETCHES


async def test_read_window_caps_and_says_so():
    _, t = tools()
    got = await t.read_window("pkg/proxy/route.go", HEAD, 1, 500)
    assert got["truncated"] is True
    assert got["end_line"] == WINDOW_CAP


async def test_changed_symbols_joins_hunks_to_spans():
    changed = [ChangedFile(path="pkg/proxy/route.go", status="modified",
                           additions=2, deletions=0,
                           hunks=[Hunk(start=7, length=2), Hunk(start=1, length=1)])]
    _, t = tools(changed)
    got = await t.changed_symbols("pkg/proxy/route.go", BASE, HEAD)
    assert [c["name"] for c in got["changed_symbols"]] == ["beta"]
    # the package-clause edit is reported, not silently dropped
    assert got["changed_lines_outside_symbols"] == [[1, 1]]


async def test_changed_symbols_reports_untouched_file():
    _, t = tools([])
    got = await t.changed_symbols("pkg/proxy/route.go", BASE, HEAD)
    assert "unchanged" in got["error"]


async def test_go_sum_is_pointed_at_the_raw_hunk():
    changed = [ChangedFile(path="go.sum", status="modified", additions=4,
                           deletions=2, hunks=[Hunk(start=10, length=6)])]
    gh = FakeGitHub({}, changed)
    t = CodeTools(gh, MemoryStore(), run_id="42")
    got = await t.changed_symbols("go.sum", BASE, HEAD)
    assert got["hunks"] == [[10, 15]] and gh.get_file_calls == 0


def test_parse_hunks_reads_head_side_ranges():
    patch = "@@ -1,4 +1,6 @@\n ctx\n+added\n@@ -40 +42,3 @@\n+more\n"
    assert [(h.start, h.end) for h in parse_hunks(patch)] == [(1, 6), (42, 44)]
    assert parse_hunks(None) == []


def test_evidence_filename_is_greppable_by_path():
    name = evidence_filename(run_id="329", kind="source_file",
                             pinned_to="c58e67f8a1b2c3d4", key="pkg/proxy/mod.go")
    assert name == "ev/329/source_file/c58e67f8a1b2/pkg/proxy/mod.go"


def test_evidence_candidates_resolve_a_model_invented_filename():
    """The model cites `job-<id>.log` and the repo path. Bytes live elsewhere."""
    sha = "3c12e1c2760ede22f032d242125706f4bbd1b5fb"
    path = "cmd/ateom-microvm/snapshotcache.go"
    source = evidence_candidates(
        run_id="36795183944", kind="source_file", key=path, pinned_to=sha,
        filename=path)
    assert evidence_filename(run_id="36795183944", kind="source_file",
                             pinned_to=sha, key=path) in source
    log = evidence_candidates(
        run_id="36795183944", kind="ci_log", key="110156891160", pinned_to=sha,
        filename="job-110156891160.log")
    assert evidence_filename(run_id="36795183944", kind="ci_log",
                             pinned_to="110156891160", key="job.log") in log


def test_evidence_filename_folds_only_hostile_characters():
    name = evidence_filename(run_id="1", kind="ref_search", pinned_to="abc",
                             key='expected "x" got (y)')
    assert " " not in name and '"' not in name and "(" not in name
