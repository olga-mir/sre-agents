"""Types shared by the code-comprehension layer.

See docs/source-code-comprehension.md §4.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

SymbolKind = Literal[
    "func", "method", "test", "subtest", "type", "const", "var", "imports"
]


class Symbol(BaseModel):
    """One addressable element of a Go file.

    `name` MUST match how the test/build output addresses this element - that is
    what makes outline + failure-text a direct lookup rather than a search.
    """

    kind: SymbolKind
    name: str
    detail: Optional[str] = None  # signature, whitespace-collapsed
    start_line: int  # 1-based, inclusive
    end_line: int  # 1-based, inclusive
    children: list["Symbol"] = Field(default_factory=list)  # subtests, one level
    heuristic: bool = False  # name was inferred, not parsed (table-driven cases)


Rung = Literal["parsed", "partial", "unparsed"]


class FileOutline(BaseModel):
    """Structure of one file at one commit. Never carries file contents."""

    path: str
    sha: str
    language: str
    total_lines: int
    byte_len: int
    rung: Rung  # which fallback rung produced this (§5)
    generated: bool = False
    symbols: list[Symbol] = Field(default_factory=list)
    sample: Optional[str] = None  # head/tail sample, rung 'unparsed' only
    notes: list[str] = Field(default_factory=list)

    def flat(self) -> list[Symbol]:
        out: list[Symbol] = []
        for s in self.symbols:
            out.append(s)
            out.extend(s.children)
        return out

    def render(self) -> str:
        """Compact text form - this is what goes into the model's context."""
        lines = [f"{self.path} @ {self.sha[:12]}  ({self.total_lines} lines, {self.rung})"]
        if self.generated:
            lines.append(
                "GENERATED FILE ('Code generated ... DO NOT EDIT.') - a failure here "
                "points at the generator or its input, not at this file."
            )
        lines.extend(f"note: {n}" for n in self.notes)
        for s in self.symbols:
            detail = s.detail or ""
            lines.append(f"{s.kind:<8}{s.name}{detail}  L{s.start_line}-{s.end_line}")
            for c in s.children:
                mark = " ~" if c.heuristic else ""
                lines.append(f"          {c.name}{mark}  L{c.start_line}-{c.end_line}")
        if self.sample:
            lines.append("--- sample ---")
            lines.append(self.sample)
        return "\n".join(lines)
