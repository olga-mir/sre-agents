"""Thin async GitHub client. Every read is pinned to a sha where one applies.

No clone, no working tree: GEAP has neither
(docs/source-code-comprehension.md §0).
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional
from urllib.parse import quote

import httpx

API = "https://api.github.com"
GRAPHQL = "https://api.github.com/graphql"

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.M)

# Module-level cache: (token, expiry epoch). GitHub App installation tokens
# are ~1hr-lived and a single investigation makes many GitHub() instances
# worth of calls, so this survives across them instead of re-minting a token
# per call.
_APP_TOKEN_CACHE: Optional[tuple[str, float]] = None


class GitHubNotFound(Exception):
    """The path is not in that repo at that sha. Tools must return this as a
    payload, not raise through the graph: an uncaught 404 aborts stream_query."""

    def __init__(self, repo: str, path: str, sha: str):
        self.repo = repo
        self.path = path
        self.sha = sha
        super().__init__(f"{path} not found in {repo} at {sha[:12]}")


def _secret_manager_token(name: str) -> str:
    """Read a token out of Secret Manager.

    `name` is either a bare secret id - resolved against GOOGLE_CLOUD_PROJECT at
    its latest version - or a full `projects/*/secrets/*/versions/*` name. The
    deployed container has GOOGLE_CLOUD_PROJECT set for it (the ADK deploy
    template writes it in), so the bare form is the normal one.
    """
    # Imported here, not at module scope: this pulls in grpc and google-api-core,
    # which the offline test suite and the local CLI have no use for.
    from google.cloud import secretmanager

    if not name.startswith("projects/"):
        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        if not project:
            raise RuntimeError(
                f"E2E_AGENT_GITHUB_TOKEN_SECRET={name!r} is a bare secret id but "
                "GOOGLE_CLOUD_PROJECT is unset; use a full resource name")
        name = f"projects/{project}/secrets/{name}/versions/latest"
    client = secretmanager.SecretManagerServiceClient()
    payload = client.access_secret_version(name=name).payload.data
    return payload.decode("utf-8").strip()


def _github_app_token() -> str:
    """Mint (or reuse a cached) GitHub App installation access token.

    Same JWT-sign-and-exchange flow as ~/repos/agents/shared_tools/github/
    auth.py's get_github_app_installation_token - ported rather than
    reimplemented so both agents' GitHub App auth stays one mental model.
    Needs GITHUB_APP_ID, GITHUB_APP_INSTALLATION_ID, and the private key PEM
    from either GITHUB_APP_PRIVATE_KEY (raw) or GITHUB_APP_PRIVATE_KEY_SECRET
    (a Secret Manager secret id, via _secret_manager_token).
    """
    import jwt  # PyJWT; only imported on the App-auth path

    global _APP_TOKEN_CACHE
    if _APP_TOKEN_CACHE:
        token, expires_at = _APP_TOKEN_CACHE
        if time.time() < expires_at - 120:  # 2-minute safety buffer
            return token

    app_id = os.environ["GITHUB_APP_ID"]
    installation_id = os.environ["GITHUB_APP_INSTALLATION_ID"]

    private_key_pem = os.environ.get("GITHUB_APP_PRIVATE_KEY", "").strip()
    if not private_key_pem:
        secret = os.environ.get("GITHUB_APP_PRIVATE_KEY_SECRET")
        if not secret:
            raise RuntimeError(
                "GITHUB_APP_ID is set but neither GITHUB_APP_PRIVATE_KEY nor "
                "GITHUB_APP_PRIVATE_KEY_SECRET is")
        private_key_pem = _secret_manager_token(secret)

    now = int(time.time())
    signed_jwt = jwt.encode(
        {"iat": now - 60, "exp": now + 600, "iss": str(app_id)},
        private_key_pem,
        algorithm="RS256",
    )

    r = httpx.post(
        f"{API}/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {signed_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        # Sync, on the critical path of pinning a run, and httpx defaults to
        # no timeout at all.
        timeout=30.0,
    )
    r.raise_for_status()
    data = r.json()

    expires_at = datetime.fromisoformat(
        data["expires_at"].replace("Z", "+00:00")).timestamp()
    _APP_TOKEN_CACHE = (data["token"], expires_at)
    return data["token"]


def _token() -> str:
    """The GitHub token, from the environment, a GitHub App, or Secret Manager.

    Deliberately no `gh auth token` fallback. GEAP runs this in a container with
    no gh CLI and no interactive login, and an auth path that only works on a
    laptop is an auth path that fails in production - silently, at the first
    node that touches GitHub. `task setup` writes GITHUB_TOKEN into .env once so
    local ergonomics are unchanged.
    """
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    if os.environ.get("GITHUB_APP_ID") and os.environ.get("GITHUB_APP_INSTALLATION_ID"):
        return _github_app_token()
    if secret := os.environ.get("E2E_AGENT_GITHUB_TOKEN_SECRET"):
        return _secret_manager_token(secret)
    raise RuntimeError(
        "no GitHub token: set GITHUB_TOKEN, GITHUB_APP_ID+"
        "GITHUB_APP_INSTALLATION_ID, or point E2E_AGENT_GITHUB_TOKEN_SECRET "
        "at a Secret Manager secret")


@dataclass
class Hunk:
    """One @@ hunk, in HEAD-side line numbers."""

    start: int
    length: int

    @property
    def end(self) -> int:
        return self.start + max(self.length, 1) - 1


@dataclass
class ChangedFile:
    path: str
    status: str
    additions: int
    deletions: int
    hunks: list[Hunk] = field(default_factory=list)
    previous_path: Optional[str] = None


def parse_hunks(patch: Optional[str]) -> list[Hunk]:
    """HEAD-side line ranges from a unified diff. No patch text is retained."""
    if not patch:
        return []
    return [
        Hunk(start=int(m.group(3)), length=int(m.group(4) or 1))
        for m in _HUNK_RE.finditer(patch)
    ]


class GitHub:
    def __init__(self, repo: str, token: Optional[str] = None, timeout: float = 30.0):
        self.repo = repo
        self._token = token or _token()
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "e2e-agent/0.1",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "GitHub":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def _get(self, path: str, **kwargs: Any) -> httpx.Response:
        r = await self._client.get(f"{API}{path}", **kwargs)
        r.raise_for_status()
        return r

    async def _paged(self, path: str, params: Optional[dict] = None) -> list[dict]:
        out: list[dict] = []
        page = 1
        while True:
            p = dict(params or {}, per_page=100, page=page)
            r = await self._get(path, params=p)
            batch = r.json()
            if isinstance(batch, dict):  # /jobs returns {"jobs": [...]}
                batch = batch.get("jobs", [])
            out.extend(batch)
            if len(batch) < 100:
                return out
            page += 1

    # ---- source -----------------------------------------------------------

    async def get_file(self, path: str, sha: str, *, repo: Optional[str] = None) -> bytes:
        """Raw bytes of one path at one commit.

        `repo` overrides `self.repo` so a fork PR's head sha can be fetched
        from `head_repository` when the commit is not in the base repo.
        """
        repo = repo or self.repo
        encoded = quote(path, safe="/")
        r = await self._client.get(
            f"{API}/repos/{repo}/contents/{encoded}",
            params={"ref": sha},
            headers={"Accept": "application/vnd.github.raw"},
        )
        if r.status_code == 404:
            raise GitHubNotFound(repo, path, sha)
        r.raise_for_status()
        return r.content

    async def blame(self, path: str, sha: str, start: int, end: int) -> list[dict]:
        """Which commit last touched each line range. GraphQL only - the REST
        API has no blame endpoint."""
        query = """
        query($owner:String!,$name:String!,$sha:String!,$path:String!){
          repository(owner:$owner,name:$name){
            object(expression:$sha){
              ... on Commit {
                blame(path:$path){
                  ranges{ startingLine endingLine
                    commit{ oid messageHeadline committedDate
                            author{ name } } } } } } } }
        """
        owner, name = self.repo.split("/", 1)
        r = await self._client.post(
            GRAPHQL,
            json={"query": query,
                  "variables": {"owner": owner, "name": name, "sha": sha, "path": path}},
        )
        r.raise_for_status()
        payload = r.json()
        if payload.get("errors"):
            raise RuntimeError(f"blame failed: {payload['errors']}")
        obj = payload["data"]["repository"]["object"]
        if not obj:
            raise RuntimeError(f"no commit {sha} in {self.repo}")
        out = []
        for rng in obj["blame"]["ranges"]:
            if rng["endingLine"] < start or rng["startingLine"] > end:
                continue
            c = rng["commit"]
            out.append({
                "start_line": rng["startingLine"],
                "end_line": rng["endingLine"],
                "sha": c["oid"],
                "headline": c["messageHeadline"],
                "author": (c.get("author") or {}).get("name"),
                "committed_at": c["committedDate"],
            })
        return out

    # ---- pull requests ----------------------------------------------------

    async def compare_files(self, base_sha: str, head_sha: str) -> list[ChangedFile]:
        """Changed files between two SHAS - the sha-pinned manifest.

        Preferred over pr_files: a PR's file list is served at the PR's CURRENT
        head, which has usually moved on from the sha the run built, so its hunk
        line numbers can be stale against the pinned outline. This also works on
        push-triggered runs, which have no PR at all.

        GitHub caps the response at 300 files; `truncated` on the result says so.
        """
        r = await self._get(f"/repos/{self.repo}/compare/{base_sha}...{head_sha}",
                            params={"per_page": 100})
        payload = r.json()
        files = payload.get("files") or []
        out = [
            ChangedFile(
                path=f["filename"], status=f["status"],
                additions=f["additions"], deletions=f["deletions"],
                hunks=parse_hunks(f.get("patch")),
                previous_path=f.get("previous_filename"),
            )
            for f in files
        ]
        return out

    async def pr_files(self, pr_number: int) -> list[ChangedFile]:
        """NOT sha-pinned: served at the PR's current head. Use compare_files
        for anything that joins against an outline."""
        raw = await self._paged(f"/repos/{self.repo}/pulls/{pr_number}/files")
        return [
            ChangedFile(
                path=f["filename"],
                status=f["status"],
                additions=f["additions"],
                deletions=f["deletions"],
                hunks=parse_hunks(f.get("patch")),
                previous_path=f.get("previous_filename"),
            )
            for f in raw
        ]

    async def pr_file_patch(self, pr_number: int, path: str) -> Optional[str]:
        """Raw hunks for one path - used only for go.mod / go.sum (§3)."""
        for f in await self._paged(f"/repos/{self.repo}/pulls/{pr_number}/files"):
            if f["filename"] == path:
                return f.get("patch")
        return None

    async def get_pr(self, pr_number: int) -> dict:
        return (await self._get(f"/repos/{self.repo}/pulls/{pr_number}")).json()

    async def prs_for_commit(self, sha: str) -> list[dict]:
        """Fork PRs have an empty run['pull_requests']; this is the fallback."""
        return (await self._get(f"/repos/{self.repo}/commits/{sha}/pulls")).json()

    # ---- actions ----------------------------------------------------------

    async def prs_for_branch(self, head_owner: str, branch: str) -> list[dict]:
        """PRs whose head is {owner}:{branch} - the fork-PR fallback."""
        r = await self._get(f"/repos/{self.repo}/pulls",
                            params={"state": "all", "head": f"{head_owner}:{branch}",
                                    "per_page": 5})
        return r.json()

    async def list_workflow_runs(
        self,
        *,
        status: Optional[str] = None,
        event: Optional[str] = None,
        per_page: int = 50,
        max_pages: int = 8,
    ) -> list[dict]:
        """Recent Actions runs for this repo. `status` is GitHub's run status
        (`completed`, `success`, `failure`, …), not conclusion."""
        out: list[dict] = []
        params: dict[str, Any] = {"per_page": per_page}
        if status:
            params["status"] = status
        if event:
            params["event"] = event
        for page in range(1, max_pages + 1):
            r = await self._get(
                f"/repos/{self.repo}/actions/runs",
                params={**params, "page": page},
            )
            payload = r.json()
            batch = payload.get("workflow_runs", []) if isinstance(payload, dict) else payload
            out.extend(batch)
            if len(batch) < per_page:
                break
        return out

    async def get_run(self, run_id: str | int) -> dict:
        return (await self._get(f"/repos/{self.repo}/actions/runs/{run_id}")).json()

    async def run_jobs(self, run_id: str | int) -> list[dict]:
        return await self._paged(f"/repos/{self.repo}/actions/runs/{run_id}/jobs")

    async def job_log(self, job_id: int) -> str:
        r = await self._client.get(
            f"{API}/repos/{self.repo}/actions/jobs/{job_id}/logs",
            follow_redirects=True,
        )
        r.raise_for_status()
        return r.text

    async def resolve_ref(self, ref: str) -> str:
        r = await self._get(f"/repos/{self.repo}/commits/{ref}",
                            headers={"Accept": "application/vnd.github.sha"})
        return r.text.strip()
