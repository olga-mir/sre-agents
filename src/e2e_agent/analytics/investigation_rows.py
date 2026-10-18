"""Map a local investigation dump (or live graph state) to warehouse rows.

Outputs and outcomes live here. Inputs for the same run are github_runs /
github_jobs — join on (repo, run_id).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .github_rows import utcnow_iso

_ASSERTION_CAP = 2000
_REASONING_CAP = 8000


def _clip(value: Optional[str], n: int) -> Optional[str]:
    if value is None:
        return None
    if len(value) <= n:
        return value
    return value[: n - 1] + "…"


def investigation_id_for_dump(run_id: str) -> str:
    return f"local:{run_id}"


def investigation_id_for_session(session_id: str) -> str:
    return f"session:{session_id}"


def investigation_row(*, investigation_id: str, repo: str, run_id: str,
                      session_id: str = "", trigger_source: str = "TEST_OPERATOR",
                      paused: bool = False, dump_path: str = "",
                      source: str = "local_dump",
                      ingested_at: Optional[str] = None) -> dict[str, Any]:
    return {
        "investigation_id": investigation_id,
        "repo": repo,
        "run_id": str(run_id),
        "session_id": session_id,
        "trigger_source": trigger_source,
        "paused": paused,
        "source": source,
        "dump_path": dump_path,
        "ingested_at": ingested_at or utcnow_iso(),
    }


def finding_row(summary: dict[str, Any], *, investigation_id: str,
                ingested_at: Optional[str] = None) -> dict[str, Any]:
    finding = summary.get("finding") or {}
    baseline = finding.get("baseline") or {}
    budget = summary.get("budget") or {}
    evidence = finding.get("evidence") or []
    return {
        "investigation_id": investigation_id,
        "failure_id": summary.get("failure_id") or finding.get("failure_id") or "primary",
        "verdict": finding.get("verdict") or "INDETERMINATE",
        "failure_type": finding.get("failure_type") or "UNKNOWN",
        "failed_test": finding.get("failed_test"),
        "failed_job_names": finding.get("failed_job_names") or [],
        "confidence": float(summary.get("confidence") or finding.get("confidence") or 0.0),
        "audit_passed": bool(summary.get("audit_passed")),
        "exit_reason": summary.get("exit_reason") or "investigation_complete",
        "one_line_summary": summary.get("one_line_summary") or finding.get("one_line_summary") or "",
        "assertion_expected": _clip(finding.get("assertion_expected"), _ASSERTION_CAP),
        "assertion_actual": _clip(finding.get("assertion_actual"), _ASSERTION_CAP),
        "offending_sha": finding.get("offending_sha"),
        "offending_paths": finding.get("offending_paths") or [],
        "offending_symbols": finding.get("offending_symbols") or [],
        "baseline_search_ok": baseline.get("search_ok"),
        "baseline_present_on_base": baseline.get("present_on_base"),
        "baseline_present_on_head": baseline.get("present_on_head"),
        "baseline_supports_introduction": bool(
            baseline.get("search_ok")
            and not baseline.get("truncated")
            and baseline.get("present_on_head")
            and not baseline.get("present_on_base")
        ) if baseline else None,
        "flake_signal": finding.get("flake_signal"),
        "reasoning": _clip(finding.get("reasoning"), _REASONING_CAP),
        "audit_notes": summary.get("audit_notes") or [],
        "budget_max_turns": (budget or {}).get("max_turns"),
        "budget_current_turn": (budget or {}).get("current_turn"),
        "evidence_count": len(evidence),
        "ingested_at": ingested_at or utcnow_iso(),
    }


# Code-navigation tools. Log tools and list_changed_files name no source file
# in their arguments, so they are not a hop.
_HOP_TOOLS = frozenset({
    "outline_file",
    "outline_files",
    "read_symbol",
    "read_symbols",
    "symbol_at_line",
    "read_symbols_at_lines",
    "read_window",
    "changed_symbols",
    "blame_lines",
})
_BATCH_HOP_TOOLS = frozenset({
    "outline_files",
    "read_symbols",
    "read_symbols_at_lines",
})


def _call_args(call: Any) -> dict[str, Any]:
    raw = call.get("args") if isinstance(call, dict) else getattr(call, "args", None)
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return dict(raw)
    except (TypeError, ValueError):
        return {}


def session_trace(events: Any) -> list[dict[str, Any]]:
    """ADK session events → the {node, calls} shape path_hop_rows reads."""
    trace: list[dict[str, Any]] = []
    for event in events or []:
        getter = getattr(event, "get_function_calls", None)
        if not getter:
            continue
        calls = []
        for call in getter() or []:
            name = getattr(call, "name", None) or ""
            if not name:
                continue
            calls.append({"name": name, "args": _call_args(call)})
        if calls:
            trace.append({
                "node": getattr(event, "author", None) or "",
                "calls": calls,
            })
    return trace


def _hop_targets(name: str, args: dict[str, Any]) -> list[dict[str, Any]]:
    if name in _BATCH_HOP_TOOLS:
        targets = args.get("targets") or []
        return [t for t in targets if isinstance(t, dict) and t.get("path")]
    path = args.get("path")
    if not isinstance(path, str) or not path:
        return []
    return [args]


def path_hop_rows(trace: list[dict[str, Any]], *, investigation_id: str,
                  ingested_at: Optional[str] = None) -> list[dict[str, Any]]:
    """One row per source file a code-navigation tool addressed, in call order.

    Batched tools explode into one row per target, so a file opened inside
    outline_files still shows up when paths are counted across investigations.
    """
    ingested = ingested_at or utcnow_iso()
    rows: list[dict[str, Any]] = []
    seq = 0
    for event in trace:
        for call in event.get("calls") or []:
            name = call.get("name") or ""
            if name not in _HOP_TOOLS:
                continue
            for target in _hop_targets(name, call.get("args") or {}):
                path = target.get("path")
                if not isinstance(path, str) or not path:
                    continue
                symbol = target.get("symbol")
                sha = target.get("sha")
                seq += 1
                rows.append({
                    "investigation_id": investigation_id,
                    "seq": seq,
                    "tool_name": name,
                    "path": path,
                    "sha": sha if isinstance(sha, str) and sha else None,
                    "symbol": symbol if isinstance(symbol, str) and symbol else None,
                    "ingested_at": ingested,
                })
    return rows


def rows_from_dump(dump_dir: Path, *,
                   repo: str = "",
                   session_id: str = "",
                   paused: bool = False) -> dict[str, list[dict[str, Any]]]:
    """Read runs/<run_id>/{summaries.json,trace.jsonl} into warehouse tables."""
    dump_dir = Path(dump_dir)
    run_id = dump_dir.name
    summaries_path = dump_dir / "summaries.json"
    summaries = json.loads(summaries_path.read_text()) if summaries_path.is_file() else {}
    trace: list[dict[str, Any]] = []
    trace_path = dump_dir / "trace.jsonl"
    if trace_path.is_file():
        for line in trace_path.read_text().splitlines():
            if line.strip():
                trace.append(json.loads(line))

    if not repo:
        for event in trace:
            out = event.get("output")
            if isinstance(out, dict) and out.get("repo"):
                repo = out["repo"]
                break
    repo = repo or "unknown"

    iid = investigation_id_for_dump(run_id)
    investigations = [investigation_row(
        investigation_id=iid, repo=repo, run_id=run_id,
        session_id=session_id, paused=paused,
        dump_path=str(dump_dir),
    )]
    findings = [finding_row(s, investigation_id=iid) for s in summaries.values()]
    return {
        "investigations": investigations,
        "findings": findings,
        "path_hops": path_hop_rows(trace, investigation_id=iid),
    }
