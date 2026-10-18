"""Turn Go failure output into addresses the outline can resolve.

Every Go failure mode puts an address in the log - a test name, a file:line, a
panic frame - so retrieval here is address lookup, not search
(docs/source-code-comprehension.md §2). This matters more in Go
than elsewhere: idiomatic errors are assembled at runtime by
`fmt.Errorf("...: %w", err)`, so grepping the source for a rendered message
finds nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

# Actions log lines arrive prefixed with an ISO timestamp.
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s?")

_FAIL = re.compile(r"^\s*--- (FAIL|SKIP): (\S+)(?: \(([\d.]+)s\))?")
_PKG_RESULT = re.compile(r"^(?:ok|FAIL|---)?\s*(?:FAIL|ok)\s+(\S+)\s")
_FILE_LINE = re.compile(r"(?:^|\s)([\w./-]+\.go):(\d+)(?::(\d+))?:")
_PANIC_FRAME = re.compile(r"^([\w./-]+)\.(\(\*?[\w]+\)\.)?(\w+)\(")
_RUN = re.compile(r"^\s*=== (?:RUN|CONT|PAUSE)\s+(\S+)")


@dataclass
class FailureAddress:
    """One resolvable address lifted from failure output."""

    kind: str  # "test" | "file_line" | "panic_frame"
    raw: str
    test: Optional[str] = None  # "TestReconcile"
    subtest: Optional[str] = None  # "missing_backend" (already Go-normalised)
    package: Optional[str] = None  # import path, e.g. github.com/o/r/pkg/proxy
    path_hint: Optional[str] = None  # "actor_test.go" or a repo-relative path
    line: Optional[int] = None
    symbol: Optional[str] = None  # "(*Server).handleAuth"
    context: list[str] = field(default_factory=list)

    @property
    def test_address(self) -> Optional[str]:
        if self.test is None:
            return None
        return f"{self.test}/{self.subtest}" if self.subtest else self.test


def strip_timestamps(log: str) -> str:
    return "\n".join(_TS.sub("", ln) for ln in log.splitlines())


def package_to_path(import_path: str, module: Optional[str]) -> Optional[str]:
    """Repo-relative directory for a Go import path, given the module path."""
    if not module:
        return None
    if import_path == module:
        return ""
    if import_path.startswith(module + "/"):
        return import_path[len(module) + 1 :]
    return None


def parse_go_failures(log: str, module: Optional[str] = None) -> list[FailureAddress]:
    """Addresses from one job's log, in the order they appear.

    `module` is the go.mod module path; with it, `FAIL <import path>` lines
    resolve a bare `actor_test.go` to its repo-relative directory. Without it
    the path hint stays a bare filename and the caller must match it against the
    PR manifest.
    """
    lines = strip_timestamps(log).splitlines()
    out: list[FailureAddress] = []
    current_pkg: Optional[str] = None
    pending: list[FailureAddress] = []  # awaiting the package line that follows

    for i, line in enumerate(lines):
        m = _PKG_RESULT.match(line)
        if m and ("/" in m.group(1) or "." in m.group(1)):
            current_pkg = m.group(1)
            for addr in pending:
                addr.package = current_pkg
            pending = []
            continue

        m = _FAIL.match(line)
        if m and m.group(1) == "FAIL":
            name = m.group(2)
            test, _, sub = name.partition("/")
            addr = FailureAddress(
                kind="test", raw=line.strip(), test=test, subtest=sub or None,
                context=[l.strip() for l in lines[max(0, i - 6) : i] if l.strip()],
            )
            out.append(addr)
            pending.append(addr)
            continue

        m = _FILE_LINE.search(line)
        if m:
            addr = FailureAddress(
                kind="file_line", raw=line.strip(), path_hint=m.group(1),
                line=int(m.group(2)),
                context=[line.strip()],
            )
            out.append(addr)
            pending.append(addr)
            continue

        m = _PANIC_FRAME.match(line)
        if m and "(" in line:
            pkg, recv, fn = m.group(1), m.group(2), m.group(3)
            symbol = f"{recv[:-1]}.{fn}" if recv else fn
            out.append(
                FailureAddress(kind="panic_frame", raw=line.strip(),
                               package=pkg, symbol=symbol)
            )

    return out


def resolve_path(path_hint: str, candidates: Iterable[str],
                 package_dir: Optional[str] = None) -> Optional[str]:
    """Full repo-relative path for a bare filename from test output.

    Prefers the package directory reported by the `FAIL <import path>` line;
    falls back to a unique basename match against known paths (the PR manifest).
    """
    if "/" in path_hint:
        return path_hint
    if package_dir is not None:
        return f"{package_dir}/{path_hint}".lstrip("/")
    hits = [c for c in candidates if c.rsplit("/", 1)[-1] == path_hint]
    return hits[0] if len(hits) == 1 else None
