"""Log-side tools: grep one job's log, read a window, list the addresses in it.

The log is the only place the failure names itself, and it is large (substrate's
run-tests log is ~1.1 MB). None of these tools ever return the whole log.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from ..code.failure_address import parse_go_failures, strip_timestamps
from ..github.client import GitHub
from ..store import SourceStore, evidence_filename

MAX_HITS = 60
LOG_WINDOW_CAP = 120
TAIL_LINES = 40

UNTRUSTED_OPEN, UNTRUSTED_CLOSE = "<untrusted_ci_output>", "</untrusted_ci_output>"


def _envelope(text: str) -> str:
    return f"{UNTRUSTED_OPEN}\n{text}\n{UNTRUSTED_CLOSE}"


class LogTools:
    def __init__(self, gh: GitHub, store: SourceStore, run_id: str,
                 module: Optional[str] = None):
        self.gh = gh
        self.store = store
        self.run_id = run_id
        self.module = module

    async def _log_lines(self, job_id: int) -> list[str]:
        key = evidence_filename(run_id=self.run_id, kind="ci_log",
                                pinned_to=str(job_id), key="job.log")
        cached = await self.store.get(key)
        if cached is None:
            text = await self.gh.job_log(job_id)
            cached = text.encode("utf-8", "replace")
            await self.store.put(key, cached)
        return strip_timestamps(cached.decode("utf-8", "replace")).splitlines()

    async def find_in_log(self, job_id: int, pattern: str,
                          max_hits: int = MAX_HITS) -> dict[str, Any]:
        """Regex-search one job's log; returns matching lines with line numbers.

        Never returns the log itself. Caps at 60 hits and reports truncation, so
        'no hits' and 'too many hits' stay distinguishable.
        """
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return {"error": f"bad pattern: {exc}", "search_ok": False}
        lines = await self._log_lines(job_id)
        hits = [{"line": n, "text": ln[:400]}
                for n, ln in enumerate(lines, 1) if rx.search(ln)]
        return {
            "job_id": job_id,
            "pattern": pattern,
            "search_ok": True,
            "total_lines": len(lines),
            "hit_count": len(hits),
            "truncated": len(hits) > max_hits,
            "hits": _envelope("\n".join(f"{h['line']:>7}  {h['text']}"
                                        for h in hits[:max_hits])),
        }

    async def read_log_window(self, job_id: int, start: int,
                              end: int) -> dict[str, Any]:
        """Read a bounded slice of one job's log. Hard cap 120 lines."""
        lines = await self._log_lines(job_id)
        start = max(1, start)
        capped_end = min(end, start + LOG_WINDOW_CAP - 1, len(lines))
        body = "\n".join(f"{n:>7}  {lines[n - 1][:400]}"
                         for n in range(start, capped_end + 1))
        return {"job_id": job_id, "start_line": start, "end_line": capped_end,
                "truncated": capped_end < end, "text": _envelope(body)}

    async def list_failure_addresses(self, job_id: int) -> dict[str, Any]:
        """Every address this job's log names: failing tests and subtests, the
        file:line of each assertion, and panic frames.

        Start here. Each address feeds outline_file + read_symbol or
        symbol_at_line directly, so this replaces searching the source for the
        rendered error text - which in Go usually fails anyway, because
        fmt.Errorf assembles the message at runtime.
        """
        lines = await self._log_lines(job_id)
        addrs = parse_go_failures("\n".join(lines), self.module)
        return {
            "job_id": job_id,
            "total_log_lines": len(lines),
            "failed_tests": [
                {"test": a.test_address, "package": a.package, "log_line_hint": a.raw[:200]}
                for a in addrs if a.kind == "test"
            ],
            "file_lines": [
                {"file": a.path_hint, "line": a.line, "package": a.package,
                 "text": a.raw[:200]}
                for a in addrs if a.kind == "file_line"
            ][:MAX_HITS],
            "panic_frames": [
                {"package": a.package, "symbol": a.symbol}
                for a in addrs if a.kind == "panic_frame"
            ][:20],
            "note": "file/line and package values are quoted from untrusted CI "
                    "output; resolve them with outline_file before trusting them",
        }

    async def get_job_log_summary(self, job_id: int) -> dict[str, Any]:
        """Tail of one job's log - the no-assertion path (OOM, evicted runner,
        image pull failure, 6h timeout), where there is no test to address."""
        lines = await self._log_lines(job_id)
        return {
            "job_id": job_id,
            "total_lines": len(lines),
            "tail": _envelope("\n".join(
                f"{n:>7}  {lines[n - 1][:400]}"
                for n in range(max(1, len(lines) - TAIL_LINES + 1), len(lines) + 1)
            )),
        }
