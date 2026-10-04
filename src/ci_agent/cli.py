"""Local harness: run the code-comprehension tools for e2e-agent.

Deliberately not an agent. Every step here is deterministic, so a disagreement
between the design and reality shows up as a wrong path or an unresolved symbol
rather than as a model's opinion.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Optional

from .env import load_dotenv
from .code.failure_address import FailureAddress, package_to_path, parse_go_failures, resolve_path
from .github.client import GitHub
from .run_context import RunContext, pin_run_context
from .store import DiskStore
from .tools.code_tools import CodeTools

DEFAULT_CACHE = os.path.expanduser("~/.cache/ci-agent")


def _tokens(text: str) -> int:
    """Rough token count - 4 chars/token. Good enough for an order-of-magnitude
    claim, and honest about being an estimate."""
    return len(text) // 4


async def _tools(repo: str, run_id: str, cache: str) -> tuple[GitHub, CodeTools]:
    gh = GitHub(repo)
    return gh, CodeTools(gh, DiskStore(cache), run_id=run_id)


async def cmd_pin(args) -> None:
    async with GitHub(args.repo) as gh:
        rc = await pin_run_context(gh, args.run)
        print(rc.model_dump_json(indent=2))


async def cmd_failures(args) -> None:
    async with GitHub(args.repo) as gh:
        rc = await pin_run_context(gh, args.run)
        for job in rc.failed_jobs:
            log = await gh.job_log(job.job_id)
            addrs = parse_go_failures(log, rc.module)
            print(f"\n=== job {job.job_id} {job.name} ({job.conclusion}) "
                  f"[{len(log)} bytes of log]")
            for a in addrs:
                if a.kind == "test":
                    print(f"  test        {a.test_address}  pkg={a.package}")
                elif a.kind == "file_line":
                    print(f"  file_line   {a.path_hint}:{a.line}  pkg={a.package}")
                else:
                    print(f"  panic_frame {a.package}.{a.symbol}")


async def _resolve_sha(gh: GitHub, sha: Optional[str]) -> str:
    if sha and sha.strip():
        return sha.strip()
    return await gh.resolve_ref("HEAD")


async def cmd_outline(args) -> None:
    if not args.path:
        print("error: --path argument is required (e.g. task debug:outline PATH=main.go)")
        return
    gh, tools = await _tools(args.repo, args.run or "adhoc", args.cache)
    async with gh:
        sha = await _resolve_sha(gh, args.sha)
        result = await tools.outline_file(args.path, sha)
        print(result.pop("outline"))
        print(json.dumps(result, indent=2))


async def cmd_symbol(args) -> None:
    if not args.path:
        print("error: --path argument is required")
        return
    gh, tools = await _tools(args.repo, args.run or "adhoc", args.cache)
    async with gh:
        sha = await _resolve_sha(gh, args.sha)
        print(json.dumps(await tools.read_symbol(args.path, sha, args.name),
                         indent=2))


async def cmd_at_line(args) -> None:
    if not args.path:
        print("error: --path argument is required")
        return
    gh, tools = await _tools(args.repo, args.run or "adhoc", args.cache)
    async with gh:
        sha = await _resolve_sha(gh, args.sha)
        print(json.dumps(await tools.symbol_at_line(args.path, sha, args.line),
                         indent=2))


async def cmd_changed(args) -> None:
    gh, tools = await _tools(args.repo, args.run or "adhoc", args.cache)
    async with gh:
        sha = await _resolve_sha(gh, args.sha)
        base = await _resolve_sha(gh, getattr(args, "base", None) or "main")
        if args.path:
            print(json.dumps(
                await tools.changed_symbols(args.path, base, sha), indent=2))
        else:
            print(json.dumps(
                await tools.list_changed_files(base, sha), indent=2))


async def cmd_blame(args) -> None:
    if not args.path:
        print("error: --path argument is required")
        return
    gh, tools = await _tools(args.repo, args.run or "adhoc", args.cache)
    async with gh:
        sha = await _resolve_sha(gh, args.sha)
        print(json.dumps(
            await tools.blame_lines(args.path, sha, args.start, args.end),
            indent=2))



def _pick_path(addr: FailureAddress, rc: RunContext,
               manifest: list[str]) -> Optional[str]:
    pkg_dir = package_to_path(addr.package, rc.module) if addr.package else None
    return resolve_path(addr.path_hint or "", manifest, pkg_dir)


async def cmd_triage(args) -> None:
    """locate -> fetch once -> outline -> read a span, on a real failed run."""
    gh, tools = await _tools(args.repo, args.run, args.cache)
    async with gh:
        rc = await pin_run_context(gh, args.run)
        print(f"run {rc.run_id}  head={rc.head_sha[:12]}  base={rc.base_sha[:12]} "
              f"({rc.base_ref_name})  pr={rc.pr_number}  module={rc.module}")
        print(f"failed jobs: {[j.name for j in rc.failed_jobs]}")

        changed = await tools.list_changed_files(rc.base_sha, rc.head_sha)
        manifest = [f["path"] for f in changed["files"]]
        print(f"changed vs {rc.base_ref_name}: {changed['file_count']} files "
              f"(pr={rc.pr_number})")

        outline_tokens = whole_file_tokens = 0
        span_tokens: dict[tuple[str, int, int], int] = {}
        seen_files: set[str] = set()

        for job in rc.failed_jobs:
            log = await gh.job_log(job.job_id)
            addrs = parse_go_failures(log, rc.module)
            failures = [a for a in addrs if a.kind == "test"]
            print(f"\n=== job {job.name}: {len(failures)} test failure(s)")

            for fail in failures[: args.limit]:
                print(f"\n--- FAIL: {fail.test_address}   pkg={fail.package}")
                # The assertion line reported for this package is the file:line.
                near = [a for a in addrs
                        if a.kind == "file_line" and a.package == fail.package]
                target = near[0] if near else None
                path = _pick_path(target, rc, manifest) if target else None
                if path is None:
                    pkg_dir = package_to_path(fail.package or "", rc.module)
                    print(f"    no file:line address; package dir = {pkg_dir}")
                    continue

                print(f"    address: {path}:{target.line}")
                outline = await tools.outline_file(path, rc.head_sha)
                if path not in seen_files:
                    seen_files.add(path)
                    outline_tokens += _tokens(outline["outline"])
                    src = await tools._source(path, rc.head_sha)
                    whole_file_tokens += _tokens(src.decode("utf-8", "replace"))
                print(f"    outline: {outline['total_lines']} lines -> "
                      f"{outline['symbol_count']} symbols "
                      f"({_tokens(outline['outline'])} tok, rung={outline['rung']})")

                # Name beats line: the subtest name from `--- FAIL:` addresses a
                # far smaller span than the assertion line, which lands in the
                # shared t.Run body of a table-driven test.
                spans = []
                by_name = await tools.read_symbol(path, rc.head_sha,
                                                 fail.test_address or "")
                if "symbol" in by_name:
                    spans.append(("read_symbol", by_name))
                    print(f"    read_symbol({fail.test_address!r}) -> "
                          f"{by_name['symbol']} L{by_name['start_line']}-"
                          f"{by_name['end_line']} ({_tokens(by_name['text'])} tok)"
                          f"{' [name inferred]' if by_name['heuristic_name'] else ''}")
                covered = ("symbol" in by_name
                           and by_name["start_line"] <= target.line <= by_name["end_line"])
                if not covered:
                    by_line = await tools.symbol_at_line(path, rc.head_sha, target.line)
                    if "text" in by_line:
                        spans.append(("symbol_at_line", by_line))
                        print(f"    symbol_at_line({target.line}) -> "
                              f"{by_line['symbol']} L{by_line['start_line']}-"
                              f"{by_line['end_line']} ({_tokens(by_line['text'])} tok)")
                for _, sp in spans:
                    key = (path, sp["start_line"], sp["end_line"])
                    span_tokens[key] = _tokens(sp["text"])

                if path in manifest:
                    ch = await tools.changed_symbols(path, rc.base_sha, rc.head_sha)
                    names = [c["name"] for c in ch.get("changed_symbols", [])]
                    print(f"    changed_symbols: {names or 'none in this file'}")
                else:
                    print("    changed_symbols: file not touched by this change")

        spans_total = sum(span_tokens.values())
        print(f"\n=== context accounting ({len(seen_files)} file(s) touched, "
              f"{len(span_tokens)} distinct span(s))")
        print(f"  whole files (current behaviour): ~{whole_file_tokens} tok")
        print(f"  outline + spans (this design):   ~{outline_tokens + spans_total} tok"
              f"  ({outline_tokens} outline + {spans_total} spans)")
        if outline_tokens + spans_total:
            ratio = whole_file_tokens / (outline_tokens + spans_total)
            print(f"  reduction: {ratio:.1f}x  "
                  f"(the design's ~15x assumes 3 000-line files; small files win less)")


async def cmd_investigate(args) -> None:
    """Run the whole graph against a real failed run."""
    from .agent.run_local import investigate

    result = await investigate(args.repo, args.run, feedback=args.feedback,
                               out_dir=args.out)
    print("\n=== final state ===")
    from .agent.state import InvestigationSummary
    from .agent.nodes import render_report

    for failure_id, summary in (result["summaries"] or {}).items():
        print(f"--- {failure_id}  audit_passed={summary.get('audit_passed')} "
              f"exit={summary.get('exit_reason')}")
        for note in summary.get("audit_notes") or []:
            print(f"    audit note: {note}")
        print(render_report(InvestigationSummary(**summary)))
    if result.get("out_dir"):
        print(f"\nwritten to {result['out_dir']}/ "
              "(report.md, summaries.json, trace.jsonl, evidence/)")
    if result["paused"]:
        print("\n(paused at dormancy_gate; pass --feedback to answer it)")


async def cmd_analytics_collect(args) -> None:
    from .analytics.collect import collect_github_runs
    from .github.client import GitHub

    async with GitHub(args.repo) as gh:
        counts = await collect_github_runs(
            gh,
            out_dir=Path(args.out),
            conclusion=None if args.conclusion in ("", "any") else args.conclusion,
            include_scans=args.include_scans,
            workflow=args.workflow or None,
            limit=args.limit,
        )
    print(f"wrote {counts['runs']} runs / {counts['jobs']} jobs to {args.out}")


async def cmd_analytics_from_dumps(args) -> None:
    from .analytics.collect import collect_investigation_dumps

    counts = collect_investigation_dumps(Path(args.dumps), out_dir=Path(args.out))
    print(f"wrote {counts} to {args.out}")


def main() -> None:
    load_dotenv()
    p = argparse.ArgumentParser(prog="ci-agent", description=__doc__)
    p.add_argument("--repo", default=os.environ.get("CI_AGENT_REPO",
                                                    "agent-substrate/substrate"))
    p.add_argument("--cache", default=DEFAULT_CACHE)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, *, run=False, sha=False, path=False, pr=False):
        sp = sub.add_parser(name)
        sp.set_defaults(fn=fn)
        if run:
            sp.add_argument("--run", required=True)
        else:
            sp.add_argument("--run", default=None)
        if sha:
            sp.add_argument("--sha", default=None)
        if path:
            sp.add_argument("--path", default=None)
        if pr:
            sp.add_argument("--pr", type=int, required=True)
        return sp

    add("pin", cmd_pin, run=True)
    add("failures", cmd_failures, run=True)
    add("outline", cmd_outline, sha=True, path=True)
    sp = add("symbol", cmd_symbol, sha=True, path=True)
    sp.add_argument("--name", required=True)
    sp = add("at-line", cmd_at_line, sha=True, path=True)
    sp.add_argument("--line", type=int, required=True)
    sp = add("changed", cmd_changed, sha=True)
    sp.add_argument("--base", default=None)
    sp.add_argument("--path", default=None)
    sp = add("blame", cmd_blame, sha=True, path=True)
    sp.add_argument("--start", type=int, required=True)
    sp.add_argument("--end", type=int, required=True)
    sp = add("triage", cmd_triage, run=True)
    sp.add_argument("--limit", type=int, default=3)
    sp = add("investigate", cmd_investigate, run=True)
    sp.add_argument("--feedback", default=None,
                    help="answer the human-in-the-loop prompt and deep-dive")
    sp.add_argument("--out", default="runs",
                    help="directory for report.md, trace.jsonl and evidence/")

    sap = sub.add_parser("analytics-collect",
                         help="write latest GitHub Actions runs as NDJSON")
    sap.set_defaults(fn=cmd_analytics_collect)
    sap.add_argument("--out", default="analytics/bq/seed")
    sap.add_argument("--conclusion", default="success")
    sap.add_argument("--workflow", default=None)
    sap.add_argument("--limit", type=int, default=25)
    sap.add_argument("--include-scans", action="store_true")

    sdp = sub.add_parser("analytics-from-dumps",
                         help="map runs/<id>/ dumps to investigation NDJSON")
    sdp.set_defaults(fn=cmd_analytics_from_dumps)
    sdp.add_argument("--dumps", default="runs")
    sdp.add_argument("--out", default="analytics/bq/seed")

    args = p.parse_args()
    asyncio.run(args.fn(args))


if __name__ == "__main__":
    main()
