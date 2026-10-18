"""Pinned run coordinates - resolved once, frozen for the run.

Pinning to a sha is required because a PR is a moving target: the run built one
commit, the branch has moved on, and a re-triage of the same run days later must
reach the same bytes and the same conclusion.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from .github.client import GitHub

FAILED_CONCLUSIONS = frozenset({"failure", "timed_out", "cancelled"})


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobRef(BaseModel):
    """A job's identity AND its name/conclusion - a bare list of ids destroys
    the corroboration signal that a second job failed the same way."""

    job_id: int
    name: str
    conclusion: Optional[str] = None
    failed_steps: list[str] = Field(default_factory=list)


class RunContext(BaseModel):
    repo: str
    run_id: str
    head_sha: str
    base_sha: str
    base_ref_name: str
    module: Optional[str] = None  # go.mod module path, for package -> dir mapping
    pr_number: Optional[int] = None
    pr_is_fork: bool = False
    head_repo: Optional[str] = None  # fork PRs: where head_sha actually lives
    jobs: list[JobRef] = Field(default_factory=list)
    jobs_observed_at: str = ""
    pinned_at: str = ""

    @property
    def failed_jobs(self) -> list[JobRef]:
        return [j for j in self.jobs if j.conclusion in FAILED_CONCLUSIONS]

    @property
    def known_shas(self) -> frozenset[str]:
        return frozenset({self.head_sha, self.base_sha})


async def read_module_path(gh: GitHub, sha: str) -> Optional[str]:
    """`module <path>` from go.mod at the pinned sha."""
    try:
        text = (await gh.get_file("go.mod", sha)).decode("utf-8", "replace")
    except Exception:
        return None
    for line in text.splitlines():
        if line.startswith("module "):
            return line.split(None, 1)[1].strip()
    return None


async def pin_run_context(gh: GitHub, run_id: str) -> RunContext:
    """Resolve a workflow run to frozen coordinates. Raises if unpinnable."""
    run = await gh.get_run(run_id)
    raw_jobs = await gh.run_jobs(run_id)  # separate endpoint; the run has no jobs array
    jobs = [
        JobRef(
            job_id=j["id"],
            name=j["name"],
            conclusion=j.get("conclusion"),
            failed_steps=[s["name"] for s in (j.get("steps") or [])
                          if s.get("conclusion") in FAILED_CONCLUSIONS],
        )
        for j in raw_jobs
    ]
    if not [j for j in jobs if j.conclusion in FAILED_CONCLUSIONS]:
        raise RuntimeError(f"run {run_id} has no failed/timed-out/cancelled jobs")

    head_sha = run["head_sha"]
    # The run's head_branch may live in a fork and not exist in this repo, so the
    # default branch - not head_branch - is the fallback baseline.
    default_branch = (run.get("repository") or {}).get("default_branch") or "main"
    pr_number, is_fork, base_ref = None, False, default_branch
    head_repo = (run.get("head_repository") or {}).get("full_name") or gh.repo
    prs = run.get("pull_requests") or []
    if not prs:
        # run['pull_requests'] is EMPTY for fork PRs - the common OSS case.
        prs = await gh.prs_for_commit(head_sha)
    if not prs:
        # And /commits/{sha}/pulls is empty too once the fork branch has moved
        # past the sha this run built. The branch itself still resolves.
        head_owner = head_repo.split("/", 1)[0]
        branch = run.get("head_branch")
        if head_owner and branch:
            prs = await gh.prs_for_branch(head_owner, branch)
    if prs:
        pr = prs[0]
        pr_number = pr["number"]
        base = pr.get("base") or {}
        base_ref = base.get("ref") or base_ref
        head = pr.get("head") or {}
        repo_field = head.get("repo")
        if repo_field and repo_field.get("full_name"):
            head_repo = repo_field["full_name"]
        is_fork = bool(head_repo and head_repo != gh.repo)

    base_sha = await gh.resolve_ref(base_ref)
    return RunContext(
        repo=gh.repo,
        run_id=str(run_id),
        head_sha=head_sha,
        base_sha=base_sha,
        base_ref_name=base_ref,
        module=await read_module_path(gh, head_sha),
        pr_number=pr_number,
        pr_is_fork=is_fork,
        head_repo=head_repo if is_fork else None,
        jobs=jobs,
        jobs_observed_at=utcnow_iso(),
        pinned_at=utcnow_iso(),
    )
