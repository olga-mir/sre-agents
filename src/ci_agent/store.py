"""Where fetched bytes live between tool calls.

A tool may not hold a process-local cache - ADK's ResumabilityConfig is explicit
that in-memory state is lost on resumption - so the bytes fetched by
outline_file are written to a durable store and re-read by read_symbol.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

_UNSAFE = str.maketrans({c: "_" for c in ' \t"\\|*?<>:^$()[]{}'})


def evidence_filename(*, run_id: str, kind: str, pinned_to: str, key: str) -> str:
    """Durable artifact name for one piece of evidence.

    Path-verbatim and therefore greppable at 3am. '/' is safe: the GCS artifact
    service disambiguates `a` from `a/b`; only shell/regex-hostile characters
    are folded to '_'.
    """
    safe_key = key.translate(_UNSAFE).strip("/")
    return f"ev/{run_id}/{kind}/{pinned_to[:12]}/{safe_key}"


def evidence_candidates(*, run_id: str, kind: str, key: str, pinned_to: str,
                        filename: str = "") -> list[str]:
    """Store names to try for one model-cited evidence ref.

    The model fills `filename` with a path or `job-<id>.log`. Bytes are stored
    under `evidence_filename`. A CI log is pinned to the job id with key
    `job.log`, even when the citation pins the ref to a commit sha and puts
    the job id in `key`.
    """
    names: list[str] = []

    def add(name: str) -> None:
        if name and name not in names:
            names.append(name)

    add(filename)
    if kind == "ci_log":
        job_id = key if key.isdigit() else pinned_to
        add(evidence_filename(run_id=run_id, kind="ci_log",
                              pinned_to=str(job_id), key="job.log"))
    elif kind == "source_file":
        path = key if key and not key.startswith("ev/") else ""
        if path:
            add(evidence_filename(run_id=run_id, kind="source_file",
                                  pinned_to=pinned_to, key=path))
    else:
        add(evidence_filename(run_id=run_id, kind=kind,
                              pinned_to=pinned_to, key=key))
    return names


@runtime_checkable
class SourceStore(Protocol):
    async def get(self, key: str) -> Optional[bytes]: ...
    async def put(self, key: str, data: bytes) -> None: ...


class MemoryStore:
    """For tests and one-shot CLI runs."""

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    async def get(self, key: str) -> Optional[bytes]:
        return self._data.get(key)

    async def put(self, key: str, data: bytes) -> None:
        self._data[key] = data


class DiskStore:
    """For the local harness: survives across processes, mirrors artifact paths."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root / key

    async def get(self, key: str) -> Optional[bytes]:
        p = self._path(key)
        return p.read_bytes() if p.is_file() else None

    async def put(self, key: str, data: bytes) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
