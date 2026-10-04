"""Go file outline via tree-sitter.

Per-file skeletons, not a repo map: the input is the bytes of one file that was
already fetched, and nothing here needs a checkout
(docs/source-code-comprehension.md §1).
"""

from __future__ import annotations

import os
import re
from typing import Optional

try:
    import certifi

    if "SSL_CERT_FILE" not in os.environ:
        os.environ["SSL_CERT_FILE"] = certifi.where()
except ImportError:
    pass

from tree_sitter import Language, Node, Parser
from tree_sitter_language_pack import get_parser

from .model import FileOutline, Symbol

GENERATED_RE = re.compile(rb"^// Code generated .* DO NOT EDIT\.$", re.M)

_TEST_PREFIXES = ("Test", "Benchmark", "Fuzz", "Example")

# A grouped const/var block longer than this is summarised rather than listed:
# 200 one-line consts is not an outline.
_MAX_SPECS_LISTED = 8

_parser: Optional[Parser] = None


def _go_parser() -> Parser:
    global _parser
    if _parser is None:
        try:
            import tree_sitter_go

            _parser = Parser(Language(tree_sitter_go.language()))
        except ImportError:
            _parser = get_parser("go")
    return _parser


def _text(node: Node, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _collapse(s: str) -> str:
    return " ".join(s.split())


def _lines(node: Node) -> tuple[int, int]:
    return node.start_point[0] + 1, node.end_point[0] + 1


def go_subtest_key(literal: str) -> str:
    """Match Go's testing.rewrite(): the name printed in `--- FAIL:` output.

    Spaces become underscores and non-printable bytes are escaped, so
    `t.Run("missing backend", ...)` shows up as `TestX/missing_backend`.
    Address lookup must normalise the same way or every multi-word subtest
    silently fails to match.
    """
    out = []
    for ch in literal:
        if ch == " ":
            out.append("_")
        elif ch.isprintable():
            out.append(ch)
        else:
            out.append("\\x%02x" % ord(ch) if ord(ch) < 0x100 else "\\u%04x" % ord(ch))
    return "".join(out)


def _unquote_go_string(raw: str) -> Optional[str]:
    """Body of an interpreted/raw Go string literal, or None if not a literal."""
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        try:
            return raw[1:-1].encode().decode("unicode_escape")
        except UnicodeDecodeError:
            return raw[1:-1]
    if len(raw) >= 2 and raw[0] == raw[-1] == "`":
        return raw[1:-1]
    return None


def _receiver_type(node: Node, src: bytes) -> Optional[str]:
    """`(*Server)` / `(Server)` for a method_declaration, as a panic frame spells it."""
    recv = node.child_by_field_name("receiver")
    if recv is None:
        return None
    for decl in recv.named_children:
        if decl.type != "parameter_declaration":
            continue
        tnode = decl.child_by_field_name("type")
        if tnode is None:
            continue
        return f"({_collapse(_text(tnode, src))})"
    return None


def _signature(node: Node, src: bytes) -> str:
    """Params and result, without repeating the name the outline already prints."""
    name_node = node.child_by_field_name("name")
    start = name_node.end_byte if name_node is not None else node.start_byte
    body = node.child_by_field_name("body")
    end = body.start_byte if body is not None else node.end_byte
    return _collapse(src[start:end].decode("utf-8", "replace"))


def _is_test_func(name: str, node: Node, src: bytes) -> bool:
    if not name.startswith(_TEST_PREFIXES):
        return False
    params = node.child_by_field_name("parameters")
    if params is None:
        return False
    ptext = _text(params, src)
    return "testing." in ptext


def _iter_calls(node: Node):
    stack = [node]
    while stack:
        cur = stack.pop()
        if cur.type == "call_expression":
            yield cur
        stack.extend(cur.named_children)


def _subtests(func_node: Node, src: bytes) -> list[Symbol]:
    """Subtest names from `t.Run` calls inside one test function.

    Subtest names are string literals passed to t.Run, not declarations, so a
    function-declaration query cannot see them - and without them the finest
    address available for a 200-line table-driven test is the whole function.
    """
    out: list[Symbol] = []
    seen: set[str] = set()
    for call in _iter_calls(func_node):
        fn = call.child_by_field_name("function")
        if fn is None or fn.type != "selector_expression":
            continue
        field = fn.child_by_field_name("field")
        if field is None or _text(field, src) != "Run":
            continue
        args = call.child_by_field_name("arguments")
        if args is None or not args.named_children:
            continue
        first = args.named_children[0]
        start, end = _lines(call)
        if first.type in ("interpreted_string_literal", "raw_string_literal"):
            literal = _unquote_go_string(_text(first, src))
            if literal is None:
                continue
            name = go_subtest_key(literal)
            heuristic = False
        else:
            # t.Run(tc.name, ...): the case names live in a table literal, not
            # here. Recover them from the enclosing function and mark them as
            # inferred - a heuristic, not a parse. The span is the case's own
            # literal, which is where the expectations that failed are written;
            # the shared body is named in `detail`.
            for name, c_start, c_end in _table_case_names(func_node, src):
                if name not in seen:
                    seen.add(name)
                    out.append(
                        Symbol(kind="subtest", name=name, detail=f" case data; "
                               f"shared body L{start}-{end}",
                               start_line=c_start, end_line=c_end, heuristic=True)
                    )
            continue
        if name in seen:
            continue
        seen.add(name)
        out.append(Symbol(kind="subtest", name=name, start_line=start, end_line=end))
    out.sort(key=lambda s: (s.start_line, s.name))
    return out


_STRING_TYPES = ("interpreted_string_literal", "raw_string_literal")
_NAME_KEYS = ("name", "Name", "desc", "testName", "title")


def _unwrap(node: Node) -> Node:
    """The Go grammar wraps composite-literal entries in `literal_element`."""
    while node.type == "literal_element" and node.named_child_count == 1:
        node = node.named_children[0]
    return node


def _table_case_names(func_node: Node, src: bytes) -> list[tuple[str, int, int]]:
    """(name, start_line, end_line) for each `name:`-keyed case literal."""
    cases: list[tuple[str, int, int]] = []
    stack = [func_node]
    while stack:
        cur = stack.pop()
        if cur.type == "keyed_element" and cur.named_child_count >= 2:
            key = _unwrap(cur.named_children[0])
            value = _unwrap(cur.named_children[1])
            if _text(key, src).strip(": ") in _NAME_KEYS and value.type in _STRING_TYPES:
                literal = _unquote_go_string(_text(value, src))
                if literal is not None:
                    # The case is the enclosing literal_value, not just this key.
                    holder = cur.parent if cur.parent is not None else cur
                    start, end = _lines(holder)
                    cases.append((go_subtest_key(literal), start, end))
        stack.extend(cur.named_children)
    cases.sort(key=lambda c: c[1])
    return cases


def _spec_symbols(decl: Node, src: bytes, kind: str, spec_type: str) -> list[Symbol]:
    specs = [c for c in decl.named_children if c.type == spec_type]
    if not specs:
        return []
    start, end = _lines(decl)
    if len(specs) > _MAX_SPECS_LISTED:
        first = specs[0].child_by_field_name("name")
        label = _text(first, src) if first is not None else "?"
        return [
            Symbol(kind=kind, name=f"{kind} block ({label}, +{len(specs) - 1} more)",
                   start_line=start, end_line=end)
        ]
    out: list[Symbol] = []
    for spec in specs:
        name_node = spec.child_by_field_name("name")
        names = (
            [name_node]
            if name_node is not None
            else [c for c in spec.named_children if c.type == "identifier"]
        )
        s_start, s_end = _lines(spec)
        for n in names:
            out.append(
                Symbol(kind=kind, name=_text(n, src), start_line=s_start, end_line=s_end)
            )
    return out


def _type_symbols(decl: Node, src: bytes) -> list[Symbol]:
    out: list[Symbol] = []
    for spec in decl.named_children:
        if spec.type not in ("type_spec", "type_alias"):
            continue
        name_node = spec.child_by_field_name("name")
        if name_node is None:
            continue
        tnode = spec.child_by_field_name("type")
        detail = None
        if tnode is not None:
            head = _collapse(_text(tnode, src))
            detail = head[:80] + (" ..." if len(head) > 80 else "")
        start, end = _lines(spec)
        out.append(
            Symbol(kind="type", name=_text(name_node, src), detail=detail,
                   start_line=start, end_line=end)
        )
    return out


def outline_go_source(source: bytes, path: str, sha: str) -> FileOutline:
    """Structure of one Go file. Returns no file contents, ever."""
    tree = _go_parser().parse(source)
    root = tree.root_node
    total_lines = source.count(b"\n") + (0 if source.endswith(b"\n") or not source else 1)
    outline = FileOutline(
        path=path,
        sha=sha,
        language="go",
        total_lines=total_lines,
        byte_len=len(source),
        rung="partial" if root.has_error else "parsed",
        generated=bool(GENERATED_RE.search(source[:4096])),
    )
    if root.has_error:
        # Tree-sitter is error-tolerant and still yields a usable partial tree -
        # which matters, because a syntax error IS the failure sometimes.
        outline.notes.append(
            "file did not parse cleanly; symbols below are what resolved"
        )

    symbols: list[Symbol] = []
    for node in root.named_children:
        if node.type == "import_declaration":
            start, end = _lines(node)
            symbols.append(Symbol(kind="imports", name="imports",
                                  start_line=start, end_line=end))
        elif node.type == "function_declaration":
            name_node = node.child_by_field_name("name")
            if name_node is None:
                continue
            name = _text(name_node, src=source)
            kind = "test" if _is_test_func(name, node, source) else "func"
            start, end = _lines(node)
            sym = Symbol(kind=kind, name=name, detail=_signature(node, source),
                         start_line=start, end_line=end)
            if kind == "test":
                sym.children = _subtests(node, source)
            symbols.append(sym)
        elif node.type == "method_declaration":
            name_node = node.child_by_field_name("name")
            if name_node is None:
                continue
            recv = _receiver_type(node, source)
            bare = _text(name_node, source)
            start, end = _lines(node)
            symbols.append(
                Symbol(kind="method", name=f"{recv}.{bare}" if recv else bare,
                       detail=_signature(node, source), start_line=start, end_line=end)
            )
        elif node.type == "type_declaration":
            symbols.extend(_type_symbols(node, source))
        elif node.type == "const_declaration":
            symbols.extend(_spec_symbols(node, source, "const", "const_spec"))
        elif node.type == "var_declaration":
            symbols.extend(_spec_symbols(node, source, "var", "var_spec"))

    symbols.sort(key=lambda s: s.start_line)
    outline.symbols = symbols
    return outline


def outline_unparsed(source: bytes, path: str, sha: str, reason: str) -> FileOutline:
    """Rung 3: not Go. total_lines plus a head/tail sample, and say so."""
    text = source.decode("utf-8", "replace")
    lines = text.splitlines()
    head, tail = lines[:20], lines[-10:] if len(lines) > 30 else []
    sample = "\n".join(head)
    if tail:
        sample += f"\n... ({len(lines) - 30} lines elided) ...\n" + "\n".join(tail)
    return FileOutline(
        path=path, sha=sha, language="unknown", total_lines=len(lines),
        byte_len=len(source), rung="unparsed", sample=sample, notes=[reason],
    )
