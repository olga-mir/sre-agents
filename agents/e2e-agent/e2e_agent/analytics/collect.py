"""Collect GitHub Actions runs (and local investigation dumps) as NDJSON."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from ..github.client import GitHub
from .github_rows import is_scan_workflow, job_row, run_row, utcnow_iso
from .investigation_rows import rows_from_dump


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, default=str) + "\n" for r in rows))


def append_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + "\n")


async def collect_github_runs(
    gh: GitHub,
    *,
    out_dir: Path,
    conclusion: Optional[str] = "success",
    include_scans: bool = False,
    workflow: Optional[str] = None,
    limit: int = 25,
    status: str = "completed",
) -> dict[str, int]:
    """Write github_runs.jsonl + github_jobs.jsonl for the latest matching runs."""
    ingested = utcnow_iso()
    status_filter = "success" if conclusion == "success" else status
    raw = await gh.list_workflow_runs(status=status_filter)
    picked: list[dict] = []
    for run in raw:
        name = run.get("name") or ""
        if workflow and name != workflow:
            continue
        if not include_scans and is_scan_workflow(name):
            continue
        if conclusion and run.get("conclusion") != conclusion:
            continue
        picked.append(run)
        if len(picked) >= limit:
            break

    run_rows = [run_row(r, ingested_at=ingested) for r in picked]
    job_rows: list[dict] = []
    for run in picked:
        jobs = await gh.run_jobs(run["id"])
        repo = (run.get("repository") or {}).get("full_name") or gh.repo
        job_rows.extend(
            job_row(j, repo=repo, run_id=str(run["id"]), ingested_at=ingested)
            for j in jobs
        )

    write_jsonl(out_dir / "github_runs.jsonl", run_rows)
    write_jsonl(out_dir / "github_jobs.jsonl", job_rows)
    return {"runs": len(run_rows), "jobs": len(job_rows)}


def collect_investigation_dumps(dumps_root: Path, *, out_dir: Path) -> dict[str, int]:
    """Turn runs/<run_id>/ dumps into investigations/findings/path_hops NDJSON."""
    tables = {"investigations": [], "findings": [], "path_hops": []}
    if not dumps_root.is_dir():
        write_jsonl(out_dir / "investigations.jsonl", [])
        write_jsonl(out_dir / "findings.jsonl", [])
        write_jsonl(out_dir / "path_hops.jsonl", [])
        return {k: 0 for k in tables}
    for child in sorted(dumps_root.iterdir()):
        if not child.is_dir() or not (child / "summaries.json").is_file():
            continue
        mapped = rows_from_dump(child)
        for key, rows in mapped.items():
            tables[key].extend(rows)
    for key, rows in tables.items():
        write_jsonl(out_dir / f"{key}.jsonl", rows)
    return {k: len(v) for k, v in tables.items()}
