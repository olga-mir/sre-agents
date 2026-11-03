"""Map GitHub Actions payloads to warehouse rows.

These are *inputs*: what CI actually did, independent of any agent verdict.
Successful runs live here as the control population for later eval.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


SCAN_WORKFLOW_MARKERS = (
    "scan",
    "github admin",
    "security",
)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_scan_workflow(name: str) -> bool:
    n = (name or "").lower()
    return any(m in n for m in SCAN_WORKFLOW_MARKERS)


def _ts(value: Optional[str]) -> Optional[str]:
    return value or None


def duration_seconds(started: Optional[str], completed: Optional[str]) -> Optional[float]:
    if not started or not completed:
        return None
    try:
        a = datetime.fromisoformat(started.replace("Z", "+00:00"))
        b = datetime.fromisoformat(completed.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (b - a).total_seconds()


def run_row(run: dict[str, Any], *, ingested_at: Optional[str] = None) -> dict[str, Any]:
    repo = (run.get("repository") or {}).get("full_name") or ""
    head_repo = (run.get("head_repository") or {}).get("full_name")
    actor = (run.get("actor") or {}).get("login")
    return {
        "repo": repo,
        "run_id": str(run["id"]),
        "workflow_id": run.get("workflow_id"),
        "workflow_name": run.get("name") or "",
        "workflow_path": run.get("path") or "",
        "event": run.get("event") or "",
        "status": run.get("status") or "",
        "conclusion": run.get("conclusion") or "",
        "run_attempt": run.get("run_attempt") or 1,
        "head_branch": run.get("head_branch") or "",
        "head_sha": run.get("head_sha") or "",
        "head_repo": head_repo or repo,
        "actor": actor or "",
        "display_title": run.get("display_title") or run.get("head_commit", {}).get("message") or "",
        "html_url": run.get("html_url") or "",
        "created_at": _ts(run.get("created_at")),
        "updated_at": _ts(run.get("updated_at")),
        "run_started_at": _ts(run.get("run_started_at")),
        "ingested_at": ingested_at or utcnow_iso(),
    }


def job_row(job: dict[str, Any], *, repo: str, run_id: str,
            ingested_at: Optional[str] = None) -> dict[str, Any]:
    steps = job.get("steps") or []
    failed = [s.get("name") or "" for s in steps
              if s.get("conclusion") in {"failure", "timed_out", "cancelled"}]
    return {
        "repo": repo,
        "run_id": str(run_id),
        "job_id": str(job["id"]),
        "job_name": job.get("name") or "",
        "status": job.get("status") or "",
        "conclusion": job.get("conclusion") or "",
        "started_at": _ts(job.get("started_at")),
        "completed_at": _ts(job.get("completed_at")),
        "duration_s": duration_seconds(job.get("started_at"), job.get("completed_at")),
        "runner_name": job.get("runner_name") or "",
        "step_count": len(steps),
        "failed_steps": failed,
        "ingested_at": ingested_at or utcnow_iso(),
    }
