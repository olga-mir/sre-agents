# Source-Code Comprehension for the CI / E2E Investigation Agent on GEAP

**Status:** active — implemented in `src/e2e_agent/`, validated against real CI
failures in [`agent-substrate/substrate`](https://github.com/agent-substrate/substrate).
See §10 for what implementation changed about the design, and
[`graph-implementation-notes.md`](./graph-implementation-notes.md) for what it
changed about the graph that hosts it.
**Graph base:** the nine-node graph in
[`graph-workflow-with-hitl.md`](./graph-workflow-with-hitl.md)
— a close replica of the e2e agent running at work. This layer slots into
`investigator_loop` and `targeted_deep_dive`; no new nodes are required.
**Scope:** **Go only.** Most failures are in Go code. YAML is deferred (§8);
Terraform/HCL is out of scope entirely.

**Supersedes** the code-reading half of an earlier design-review round that
deleted structural parsing on a bad argument (§1) — trimmed from this copy of
the repo. Everything else from that round — evidence pinning, the baseline
check, the deterministic verifier, and the untrusted-input envelope — stands
unchanged.

---

## 0. The constraint set

| Constraint | Consequence |
|---|---|
| **No `git clone` on GEAP; no persistent volume** | Every byte arrives through the GitHub API. Anything needing a working tree is out — including the review's `search_ref` clone infrastructure. |
| **The repo is big** | Whole-repo indexing is out on cost alone, independently of the clone question. |
| **An investigation reads 1–5 files** | Per-file work is cheap. Repo-wide work is neither needed nor possible. This is the key sizing fact. |
| **Current tools fetch whole files into context** | The problem to solve. A 3 000-line file is ~35k tokens; five of them is most of a context window spent on code nobody reads. |
| **Go** | One grammar, one dependency. No Python in the repo at all, so `ast` is never correct. |

The 1–5 file figure is what makes this tractable. The impossible version of code
comprehension is "understand this repository". The version you need is
"understand these five files well enough to explain a test failure", and that is
a per-file problem.

---

## 1. Why structural parsing belongs here after all

The design review deleted skeletonization. Its reasons were:

1. `ast.parse` raises on non-Python — **correct, and fatal to the original §2.**
2. Aider's repo map is PageRank over an import graph and needs a full local
   checkout — **correct.**
3. The CI-failure transcript never parsed a syntax tree — **correct, for that one
   investigation.**
4. Therefore no structural parsing — **does not follow.**

(2) is an argument against **repo maps**, not against **file skeletons**, and the
review conflated them:

| | Repo map | File skeleton |
|---|---|---|
| Question | "Which of 10 000 files matter?" | "What is in this file, and which 40 lines do I read?" |
| Input | The whole repository | The bytes of one file you already fetched |
| Needs a checkout | Yes | No |
| Needed here | No — the diff and the failure output already name the files | **Yes — this is the bottleneck** |

You do not have a file-*finding* problem. You have a file-*reading* problem.

On (3): that investigation was a case where the failing assertion string appeared
verbatim in the source. **In Go that is the exception, not the rule** — see §2.

---

## 2. The load-bearing insight: Go failure output names the symbol

Every Go failure mode puts an *address* in the log, in a form a structural
outline resolves directly:

| Failure | Log contains | Resolves to |
|---|---|---|
| Test failure | `--- FAIL: TestReconcile/missing_backend` | `func TestReconcile`, subtest `missing_backend` |
| Assertion detail | `route_test.go:412: expected X, got Y` | file + line → enclosing symbol |
| Build error | `pkg/proxy/route.go:120:5: cannot use ...` | file + line → enclosing symbol |
| Panic | `main.(*Server).handleAuth(0x...)` | method `(*Server).handleAuth` |
| Race detector | goroutine stacks with `pkg.Func()` frames | each frame → a symbol |

So the retrieval move is **address lookup, not search**: outline the file, match
the address from the failure text, read that one span. No grep, no code search,
no clone.

This is also why the review's grep-first conclusion generalised badly here.
**Go error messages are built with `fmt.Errorf("...: %w", err)`**, so the rendered
message in the log is assembled at runtime from a format string plus a wrapped
error. Grepping the source for the full rendered string finds nothing. Literal
search worked in the transcript because Rust `#[error("...")]` puts the whole
string in one place; on idiomatic Go it degrades badly. Structure is how you find
these.

**Design rule:** name symbols the way the log names them (`TestReconcile`,
`(*Server).handleAuth`), not the way the grammar names them. The outline is an
index into failure text.

---

## 3. The pipeline

Four moves: **locate → fetch once → outline → read a span.** Only the last two
put source in context, and both are bounded.

```
PR manifest ──┐
failure text ─┼──► candidate paths ──► outline_file ──► symbol table ──► read_symbol ──► ~40 lines
code search ──┘                        (bytes→artifact)   (~1.5k tok)                     in context
```

| Tool | Returns | Context cost |
|---|---|---|
| `list_changed_files(base_sha, head_sha)` | paths, status, ±counts, **hunk line ranges**. No content. Sha-pinned — see §10.1. | ~0.5k |
| `outline_file(path, sha)` | Symbol table (§4). Fetches bytes once, saves the raw file as an artifact, returns only structure. | ~1–2k for 3 000 lines |
| `read_symbol(path, sha, symbol)` | The exact span of one named symbol. | ~0.5k |
| `symbol_at_line(path, sha, line)` | The symbol *containing* a line, then its span. | ~0.5k |
| `read_window(path, sha, start, end)` | Escape hatch for non-symbol regions (imports, consts). Hard cap 120 lines. | ≤120 lines |
| `changed_symbols(path, base_sha, head_sha)` | **The join.** Intersects diff hunk ranges with symbol spans. | ~0.3k |
| `blame_lines(path, sha, start, end)` | Which commit last touched these lines, via GraphQL blame. | ~0.3k |

**Address by name, not by line.** `read_symbol(path, sha, "TestReconcile")` beats
`read_window(start=120, end=160)`: line numbers shift between base and head, symbol
names do not, so the same call works against both refs when comparing behaviour.

### `symbol_at_line` is what makes compile and assertion errors cheap

Go hands you `route.go:120:5` constantly — build errors, `t.Errorf` locations,
panic frames. The naive response is `read_window(100, 140)`, which straddles
function boundaries and gives the model half of two functions. `symbol_at_line`
maps 120 → `func classifyBackendError` (L118–160) and returns that *whole
semantic unit*. Same token cost, strictly better context. It is a lookup over the
outline you already have.

### `changed_symbols` is the highest-value diff call

It answers *"what did this PR change, in units a human reasons about"*:

> `classifyBackendError` (L120–160, modified) · `(*Server).handleAuth` (L44–92, modified)

rather than 400 lines of unified diff. A pure join over data you already fetched —
no extra API calls.

**`go.mod` / `go.sum` are an exception**: a dependency bump is a very common CI
break, and those diffs are already small and structural. Read the raw hunk from
`list_changed_files`; do not outline them.

### `blame_lines` replaces the commit walk

The [GitHub GraphQL API exposes blame](https://docs.github.com/en/graphql/reference/git#object-treeentry)
— `object(expression:)` → `blame(path:)` → `ranges { startingLine endingLine commit { oid messageHeadline author } }`.
"Which commit introduced these lines" with no clone and no working tree. The REST
API has no blame endpoint; this is GraphQL-only.

---

## 4. The Go outline

```python
class Symbol(BaseModel):
    """One addressable element of a Go file.

    `name` MUST match how the test/build output addresses this element - that is
    what makes outline + failure-text a direct lookup rather than a search.
    """
    kind: str                  # func | method | test | type | const | var
    name: str                  # "TestReconcile", "(*Server).handleAuth", "BackendError"
    detail: Optional[str]      # signature
    start_line: int
    end_line: int
    children: list["Symbol"] = []   # subtests, one level
    generated: bool = False         # see below
```

```
test  TestReconcile(t *testing.T)                       L88-140
        missing_backend                                 L96-108
        stale_endpoint                                  L110-124
func  classifyBackendError(err error) BackendErrorKind  L120-160
meth  (*Server).handleAuth(w, r)                        L44-92
type  BackendError struct {Kind, Msg, Cause}            L20-28
```

### Subtests are the highest-value extraction, and there is a naming trap

`--- FAIL: TestReconcile/missing_backend` names a subtest. Subtest names are
*string literals passed to `t.Run`*, not declarations, so a plain
function-declaration query cannot see them — and without them you can only
resolve to the parent function, which for a table-driven test is the entire
200-line block.

Extract them by matching `t.Run` calls:

```scheme
(call_expression
  function: (selector_expression field: (field_identifier) @m (#eq? @m "Run"))
  arguments: (argument_list . (interpreted_string_literal) @subtest))
```

**The trap:** Go's `testing` package rewrites subtest names before printing them —
**spaces become underscores**, and non-printable characters are escaped. So
`t.Run("missing backend", ...)` appears in the log as `TestReconcile/missing_backend`.
Address lookup must normalise the same way, or every multi-word subtest silently
fails to match:

```python
def go_subtest_key(literal: str) -> str:
    """Match Go's testing.rewrite(): spaces -> underscores."""
    return literal.replace(" ", "_")
```

For fully table-driven tests the case names live in a slice literal rather than in
`t.Run` string constants (`t.Run(tc.name, ...)`). When the `t.Run` argument is not
a literal, fall back to extracting `name:` field values from the adjacent
composite literal, and mark them lower-confidence — they are a heuristic, not a
parse.

### Flag generated files

Go repos with controllers or CRDs are full of `zz_generated.deepcopy.go`,
clientsets, and protobuf output. Detect the standard marker on the first lines:

```python
GENERATED_RE = re.compile(r"^// Code generated .* DO NOT EDIT\.$", re.M)
```

Outline them normally but set `generated=True`, and have the node instruction say
so: **a failure inside generated code points at the generator or its input, not at
the file.** Blaming a generated file is a confidently wrong answer, and the
verifier's `offending_paths` check will not catch it.

Similarly, skip `vendor/` unless a path under it is explicitly requested.

---

## 5. Packaging

One dependency, pre-built wheels, no compilation:

[`tree-sitter-language-pack`](https://pypi.org/project/tree-sitter-language-pack/)
— language id `Go` (also `Gomod`, `Gosum`, `JSON`, `Dockerfile`, `Bash`, `Make`
if ever wanted). It is the maintained successor to the unmaintained
[`tree-sitter-languages`](https://github.com/grantjenks/py-tree-sitter-languages)
— do not use the latter.

This retires the review's "vendoring a grammar per language is a maintenance
surface" objection outright: it is one pinned dependency.

**Fallback ladder** — never hard-fail:
1. `Go` grammar → symbol table from the queries above.
2. Parse error → Tree-sitter is error-tolerant and still yields a partial tree;
   return what resolved plus `partial=True`. (This matters — a syntax error *is*
   the failure sometimes, and `ast`-style all-or-nothing parsing returns nothing
   in exactly that case.)
3. Non-Go file → `total_lines` plus a head/tail sample.

Every rung returns something and reports which rung it used.

```python
async def outline_file(path: str, sha: str, tool_context: ToolContext) -> dict:
    """Fetch one Go file at one commit, store it, and return its structure only.

    NEVER returns file contents. A 3 000-line file yields ~60 lines of outline.
    Use a returned symbol name with read_symbol, or a line number from a build
    error or stack frame with symbol_at_line.

    Symbol names match how Go test/build output addresses them - 'TestX' from
    '--- FAIL: TestX', '(*T).Method' from a panic frame - so failure text can be
    looked up directly. Subtest names are normalised the way Go prints them
    (spaces become underscores).

    `sha` must be a commit sha, never a branch name: a PR is a moving target and
    a re-triage of the same run must reach the same bytes.

    Returns generated=True for files carrying the 'Code generated ... DO NOT
    EDIT.' marker - a failure there points at the generator, not the file.
    """
```

---

## 6. Why this is worth doing, in one number

A 3 000-line Go file is roughly 35k tokens.

| Approach | 5 files | Then reading 3 spans | Total |
|---|---|---|---|
| Current (dump whole files) | ~175k | — | **~175k** |
| Outline → targeted read | ~8k | ~1.5k | **~10k** |

Roughly 15×, and it compounds across turns: the outline stays small while dumped
files sit in history at full weight for the rest of the investigation.

The secondary argument is quality, not cost — "lost in the middle" is real, and a
model given a 60-line outline plus one 40-line function reasons better about that
function than one given 3 000 lines containing it.

---

## 7. What you still cannot do without a checkout

- **"Who calls this function?" across the repo.** GitHub code search indexes the
  default branch only, ~10 req/min. Cross-file call-graph reasoning is degraded.
  *Mitigation:* Go panics and race reports carry full stack frames, which is a
  call path handed to you for free — outline each frame's file and you have the
  chain without a call graph. `changed_symbols` + `blame_lines` cover the rest.
- **Whole-repo grep at a PR head ref.** Unavailable at any price without a
  checkout. Base-branch code search is the only remote option — so the review's
  Open Tradeoff 2 option (c) is now the *only* option, not the interim one, and
  options (a) and (b) should be struck.
- **Following an import to a file the diff never touched** costs a full extra
  `outline_file` round trip. Budget for 1–5 files, not 20.

---

## 8. Deferred: YAML

Not needed for the first cut — most failures are in Go. When it comes back:

- Manifests are raw or Kustomize, both of which are **valid YAML as authored**, so
  the `YAML` grammar in the same language pack parses them directly. There is no
  render-first problem (that would only arise with Helm templates).
- Useful outline shape is document-oriented: one entry per document, named
  `{kind}/{metadata.name}`, matching how `kubectl apply` reports failures.
- Kustomize overlays add one wrinkle: the failing resource is the *rendered*
  result, so a `kustomization.yaml` patch may not textually contain the failing
  field. Outline the overlay's `resources`/`patches` lists so the agent can walk
  to the base — or read the rendered output from the CI log if the pipeline
  prints it.

---

## 9. Answered questions

1. **Ephemeral `git clone` within one invocation?** **No — proceed without it.**
   Persistent storage and other GitHub connectivity routes exist, but each needs
   more research, delivery time, and buy-in from other people. Against that, an
   investigation fetches 2–6 files. The clone is not worth the organisational
   cost for the return, so §7's limits are permanent, not interim.
2. **Vendored dependencies / controller-operator repo?** `go.sum` changes appear
   occasionally; Go dependencies are **not** vendored. The `vendor/` skip and the
   `go.mod`/`go.sum` raw-hunk exception in §3 stay, but neither is load-bearing.
3. **Which repo do the POCs run against?** The work repo cannot be used from a
   personal laptop, so the fixture is
   [`agent-substrate/substrate`](https://github.com/agent-substrate/substrate) —
   Go, actively failing CI, microVM/networking/agent-runtime domain. Its
   `run-tests` job emits exactly the address shapes §2 depends on: `--- FAIL:
   TestX/sub_name`, `actor_test.go:3086:`, `FAIL <import path>`, panic frames.

---

## 10. What implementation changed

Five corrections, all found by running the tools against real failed runs.

### 10.1 The PR file list is not sha-pinned — use `compare` instead

`GET /pulls/{n}/files` is served at the PR's **current** head. On run 33262534586
the run built `5a52665a`, while the PR's head had already moved to `cd207e33`, so
its hunk line numbers do not line up with an outline of the sha the run built —
the exact class of error §3 pins shas to avoid.

`GET /compare/{base_sha}...{head_sha}` returns the same per-file hunks at pinned
shas, and works on **push-triggered runs, which have no PR at all**. So
`list_pr_files(pr_number, sha)` in §3 becomes `list_changed_files(base_sha,
head_sha)`; a PR-level view (title, author, manifest) remains available, marked
`pinned: false`, and must not be joined against an outline. Cap: 300 files.

### 10.2 Fork PRs need a third fallback

The review's ladder — `run["pull_requests"]`, then `GET /commits/{sha}/pulls` —
returned nothing for substrate's fork PRs, because the fork branch has moved past
the sha the run built. `GET /pulls?state=all&head={fork_owner}:{branch}`, built
from the run's `head_repository.full_name` and `head_branch`, resolves it. Also:
the base ref must fall back to the repo's **default branch**, never to
`head_branch` — a fork branch does not exist in the base repo and resolving it
returns 422.

### 10.3 Table-driven subtests: the useful span is the case literal

§4 predicted the `t.Run(tc.name, ...)` fallback and it fires constantly — it is
how substrate writes tests. But the span to return is not the shared `t.Run`
body: it is **the case's own composite literal**, where the expectations that
failed are written. On `podidentitysigner_test.go`,
`TestMakeCert/shorter_requested_lifetime_honored` resolves to L186–204 (19 lines
of case data) rather than the 212-line parent function. The shared body's line
range is carried in `detail` so it is still reachable.

Grammar detail: Go's composite-literal entries are wrapped in `literal_element`
nodes, so `keyed_element`'s children must be unwrapped before the string-literal
check — without that, every table-driven test silently yields zero subtests.

### 10.4 Name beats line for table-driven tests

`symbol_at_line` is right for build errors and panic frames, but the assertion
line of a table-driven failure lands in the **shared** `t.Run` body, so it
resolves to the whole parent function. When the log gives a subtest name, address
by name first and fall back to `symbol_at_line` only if the assertion line falls
outside the resolved span. On the same failure that is 229 tokens instead of
2 358.

### 10.5 Go package attribution is retroactive

`go test` prints `FAIL <import path>` **after** the failure lines it covers, so a
parser must backfill the package onto the addresses it has already emitted.
With the module path from `go.mod` at the pinned sha, that turns a bare
`actor_test.go` into `cmd/ateapi/internal/controlapi/functionaltest/actor_test.go`
without touching the manifest — which matters, because the failing file is
usually **not** in the diff.

### Measured, on real runs

| Run | File | Whole file | Outline + spans | Reduction |
|---|---|---|---|---|
| 33259998849 | `actor_test.go`, 3 251 lines | ~32.2k tok | ~2.6k tok | **12.2×** |
| 33262534586 | `podidentitysigner_test.go`, 423 lines | ~3.7k tok | ~2.9k tok | 1.3× |

§6's ~15× holds for the large-file case it was written about. The second row is
the honest other half: on a small file the outline is pure overhead, and the win
comes entirely from not re-dumping the file on later turns.
