# CI Failure Investigation — Method Report

> **What this is:** Claude investigated a real CI failure in `agentgateway`, then
> was interrogated about how it arrived at the answer. This document is that
> account — the commands, the reasoning at each branch point, and the
> recommendations that fall out of it.
>
> **Why it is here:** it is the behavioural target for the SRE agent. §2 is a
> transcript of the tool-call sequence the agent needs to reproduce; the second
> half feeds directly into how the investigator reads source — see
> [`source-code-comprehension.md`](./source-code-comprehension.md).



**Subject:** How I traced the `controller-e2e` failure in
[run 32897787235 / job 97964467088](https://github.com/agentgateway/agentgateway/actions/runs/32897787235/job/97964467088)
back to the change that introduced it.

**Audience:** Someone building an ADK agent that performs similar "triage a CI
failure against source code" tasks. The second half of this document is
recommendations for how such an agent should work with files and diffs.

---

## 1. The task, restated

Given a URL to a red GitHub Actions job, answer: *what change introduced this
failure?* That decomposes into four sub-questions, each of which gates the next:

1. **Which check failed, and on what?** (job → step → test name)
2. **What exactly is the assertion that failed?** (expected vs. actual)
3. **Is this failure new, or pre-existing on the base branch?** (compare against `main`)
4. **Which commit/PR changed the behavior the assertion depends on?** (blame the delta)

The whole investigation is just walking down that chain, and at each step
choosing the cheapest command that produces enough signal to pick the next step.

---

## 2. Chronological log of what I actually ran

Each entry: the command, why I ran it, what it told me, and what I decided next.

### Step 1 — Identify the failing job

```bash
gh run view 32897787235 --repo agentgateway/agentgateway | head -80
```

**Why:** The URL names a run and a job id, but I want the whole picture first —
which jobs failed, whether it's one flaky job or a systemic break, and the branch
name.

**Learned:**
- Branch `fix/2765-backend-auth-classification`, PR #3177.
- Two jobs red: `controller-e2e` (the one in the URL) and `controller-conformance`.
- `controller-e2e` failed in a step literally called `E2E`, exit code 1.

**Decision:** Focus on `controller-e2e` job `97964467088` as asked. Note that a
second job also failed — worth keeping in mind as corroboration that the PR
itself is the cause, not infra flake.

**`head -80`** — `gh run view` prints the full job tree plus annotations plus
artifact list. I only need the top. Capping output is the default posture, not an
afterthought (see §5).

### Step 2 — Get the failing log, first attempt (and why it under-delivered)

```bash
gh run view --job 97964467088 --repo agentgateway/agentgateway --log-failed | tail -150
```

**Why:** `--log-failed` restricts to failed steps only. I tailed it on the
assumption that a test runner prints its verdict at the end.

**Learned:** Almost nothing useful. The tail was full of `--- PASS` lines from
*other* tests and then the post-job `upload-artifact` / git-cleanup noise. Go's
`go test` streams `=== RUN` / `--- PASS` / `--- FAIL` lines interleaved as tests
run, and the single `FAIL` summary line is buried well above the trailing CI
boilerplate.

**Decision:** Stop reading by position (`tail`), start reading by *content*
(`grep`). This is the pivotal method choice of the whole investigation.

### Step 3 — Find the failure by pattern, not position

```bash
gh run view --job 97964467088 --repo agentgateway/agentgateway --log-failed \
  | grep -E "\-\-\- FAIL|FAIL:|=== RUN.*Auth|PANIC|panic:"
```

**Why:** I don't know *where* the failure is in the log, but I know what failure
lines *look like* in Go test output: `--- FAIL:`, a bare `FAIL:`, or a panic.

**Learned:**
```
--- FAIL: TestBackendAuth (30.28s)
    --- FAIL: TestBackendAuth/JwtSign (30.06s)
        --- FAIL: TestBackendAuth/JwtSign/Invalid (30.02s)
```
The `30.02s` is a tell: this is a *timeout*, i.e. a poll/retry loop that never saw
the condition it wanted — not a fast assertion mismatch.

**Decision:** I have the test name. Now I need the assertion body, which `grep`
alone won't give me because it's a multi-line block. Switch to "locate the line
number, then read a window around it."

### Step 4 — Locate the failure block's line numbers

```bash
gh run view --job 97964467088 --repo agentgateway/agentgateway --log-failed \
  | grep -nE "JwtSign|TestBackendAuth"
```

**Why:** `-n` gives line numbers *within the log stream*. This is the "build a
map" step: I want coordinates so I can slice precisely.

**Learned:** The `=== RUN` for the subtest is at line 1600, the `--- FAIL`
summary at 1634. The interesting stuff (the assertion) is between them.

### Step 5 — Read the bounded window

```bash
gh run view --job 97964467088 --repo agentgateway/agentgateway --log-failed \
  | sed -n '1600,1629p'
```

**Why:** Now that I have coordinates, print exactly that slice — nothing else.

**Learned — the actual defect:**
```
Expected <[]uint8>: "backend authentication failed: local backend authentication failure: jwtSign configuration is invalid"
to contain substring
<string>: "backend authentication failed: jwtSign configuration is invalid"
```
The response body gained an extra `local backend authentication failure: `
segment, so the substring assertion (and the 500-status + retry loop wrapped
around it) never matched, and the test timed out.

**Decision:** Now I have a concrete string delta. Next: is this string produced by
`main`, or only by the PR? Go to the source.

### Step 6 — Ground the strings in the local source tree

```bash
# first attempt — FAILED
grep -rn "jwtSign configuration is invalid\|local backend authentication failure\|backend authentication failed" \
  --include=*.rs --include=*.go | head -40
# => (eval):1: no matches found: --include=*.rs
```

That error is **zsh glob expansion**, not a grep failure: unquoted `*.rs` with no
matching file in the CWD makes zsh abort the command. Fix: quote the glob and
give `grep` an explicit path.

```bash
grep -rn "jwtSign configuration is invalid\|local backend authentication failure\|backend authentication failed" \
  --include="*.rs" --include="*.go" . | head -40
```

**Learned:**
- `crates/agentgateway/src/proxy/mod.rs:213: #[error("backend authentication failed: {0}")]`
- `controller/test/e2e/backendauth_test.go:90: gomega.ContainSubstring("backend authentication failed: jwtSign configuration is invalid")`
- The string `local backend authentication failure` **does not exist anywhere in
  the current tree.**

That last point is the crux: the failing string is *not* in `main`, so the PR
introduced it. If the string had been present on `main`, the failure would be
pre-existing and my conclusion would be different.

### Step 7 — Confirm what `main` actually produces

```bash
grep -rn "local backend authentication failure\|BackendAuthenticationFailed\|backend authentication failed" \
  crates/agentgateway/src/proxy/mod.rs
```

**Learned:** On `main`, the error type is
`BackendAuthenticationFailed(anyhow::Error)` rendered as
`#[error("backend authentication failed: {0}")]`. Feed it
`jwtSign configuration is invalid` and you get exactly the string the e2e test
expects. So `main` is green for this test; the PR is the delta.

### Step 8 — Read the PR's intent and file list

```bash
gh pr view 3177 --repo agentgateway/agentgateway --json title,author,state,headRefName,body
gh pr diff 3177 --repo agentgateway/agentgateway --name-only
```

**Why:** Before reading a diff, read the PR description — it tells you what the
author *meant* to change, which tells you which hunks matter. And `--name-only`
scopes the diff: 4 files, all under `crates/agentgateway/src/http/auth/` and
`proxy/mod.rs`. No test file among them → the author changed behavior without
updating the e2e expectation. That's the shape of the bug already.

**Learned:** PR title "auth: classify backend authentication failures". Body says
it wraps `ProxyError::BackendAuthenticationFailed` so local vs. credential-provider
failures can return 500 vs. 502. So it introduces a new wrapper enum — which is
exactly where an extra message segment would come from.

### Step 9 — Slice the diff, pass 1: build a map

```bash
gh pr diff 3177 --repo agentgateway/agentgateway \
  | grep -nE "^\+|^-|local backend authentication failure|BackendAuthError|thiserror|#\[error"
```

**Why:** `gh pr diff` is a ~380-line unified-diff blob across 4 files. I don't
want it all in context, and I can't random-access a pipe. So I first run a
`grep -n` over it to get a **table of contents**: which line ranges contain the
new enum, the `#[error(...)]` attributes, the `+`/`-` churn.

**Learned:** There's a new `enum BackendAuthError { Local(..), CredentialProvider(..) }`
near the top of `auth/mod.rs`'s hunk; `oauth/transport.rs` churn around diff-lines
160–320; the `proxy/mod.rs` hunk starts around diff-line 372.

### Step 10 — Slice the diff, pass 2: read a window by offset

```bash
gh pr diff 3177 --repo agentgateway/agentgateway | sed -n '160,320p'
```

**Why:** From pass 1 I know the OAuth transport changes live roughly here. Print
that window to see the `FetchError::Upstream` → `FetchError::Local` /
`CredentialProvider` rename and the new `classify_token_endpoint_call_error`
helper.

**Weakness of this call:** raw line offsets into a diff are brittle — if the diff
grows by one line, `160,320` points somewhere slightly different. It worked here
because I'd just computed the offsets seconds earlier from the same output. For
anything you'll re-run, prefer pass 3's technique.

### Step 11 — Slice the diff, pass 3: extract one file's hunk by regex range

```bash
gh pr diff 3177 --repo agentgateway/agentgateway \
  | sed -n '/a\/crates\/agentgateway\/src\/proxy\/mod.rs/,/^diff --git/p'
```

**Why:** This is the right way to address a multi-file diff: a unified diff is
self-delimiting — every file section starts with `diff --git a/… b/…`. So
"print from the `proxy/mod.rs` header to the next `diff --git`" extracts exactly
that file's changes regardless of size or position. Robust, re-runnable, no
magic numbers.

**Learned — the mechanism:** the PR changes
```rust
BackendAuthenticationFailed(anyhow::Error)              // was
BackendAuthenticationFailed(#[from] http::auth::BackendAuthError)   // now
```
Outer render is still `backend authentication failed: {0}`. So `{0}` is now a
`BackendAuthError`, and *its* `Display` supplies the extra segment. That's the
doubled string.

### Step 12 — Map the failing run to a specific commit

```bash
gh pr view 3177 --repo agentgateway/agentgateway \
  --json commits --jq '.commits[] | "\(.oid[0:9]) \(.messageHeadline) \(.committedDate)"'
gh api repos/agentgateway/agentgateway/actions/runs/32897787235 \
  --jq '.head_sha, .head_branch, .created_at, .event'
```

**Why:** "The PR" isn't a precise answer. Which commit? The run records the exact
`head_sha` it built.

**Learned:**
- Run built `c58e67f8` — *"refactor(auth): classify backend auth failures"*, the
  **first** commit of the PR, committed 6 minutes before the run started.
- The PR has three later commits. The immediate next one is
  `42c8541d` — *"fix(auth): preserve backend error messages"*, 32 minutes later.
  The name strongly implies it fixes this very failure.

### Step 13 — Verify the follow-up commit is the fix

```bash
gh api repos/agentgateway/agentgateway/commits/42c8541da --jq '.files[].filename'
gh api repos/agentgateway/agentgateway/commits/42c8541da \
  --jq '.files[] | select(.filename|test("auth/mod.rs|proxy/mod.rs")) | .patch'
```

**Learned:**
```rust
-  #[error("local backend authentication failure: {0}")]
-  Local(#[source] anyhow::Error),
-  #[error("credential provider failure: {0}")]
-  CredentialProvider(#[source] anyhow::Error),
+  #[error(transparent)]
+  Local(anyhow::Error),
+  #[error(transparent)]
+  CredentialProvider(anyhow::Error),
```
Confirmed. The first commit gave `BackendAuthError` variants a prefixed
`#[error(...)]` message; combined with the outer `backend authentication failed:
{0}` that produced the doubled string and broke the substring assertion. The
follow-up commit switched the variants to `#[error(transparent)]`, restoring the
original wording. The linked run simply predates that fix.

---

## 3. How I decided to slice the PR diff

You asked specifically about this. The diff got looked at four times
(steps 8–11) because a diff is a **stream you can't seek**, and pulling all of it
into working context is both wasteful and hard to reason over. So I treat it as a
locate-then-read problem, same as a source file:

| Pass | Command shape | Purpose | Re-runnable? |
|---|---|---|---|
| 8 | `--name-only` | Scope: which files, how many | yes |
| 9 | `\| grep -nE 'pattern'` | Map: line ranges of interesting hunks | yes |
| 10 | `\| sed -n 'A,Bp'` | Read window by numeric offset | **no** — offsets rot |
| 11 | `\| sed -n '/diff --git a\/PATH/,/^diff --git/p'` | Read one file's hunk by structural delimiter | yes |

**Rules of thumb I was following:**

- **Never read the whole diff if you can read one file's hunk.** Unified diffs are
  self-delimiting (`diff --git` headers, `@@` hunk headers). Address them by those
  markers, not by raw line numbers.
- **`grep -n` first to get coordinates, then a windowed read.** Two cheap commands
  beat one expensive "dump everything."
- **Numeric offsets are a last resort** — fine for a one-off you computed
  moments ago from the identical output, not for anything you might repeat or that
  an agent might cache and reuse.
- **Read the prose before the diff.** The PR body and the file list told me the
  bug's shape (behavior change + no test update) before I read a single `+` line.
- **Filter the API response, don't post-process it.** `gh api … --jq` at
  step 13 pulls just the one file's `.patch` instead of fetching the whole commit
  and grepping. Push the filter as close to the source as possible.

---

## 4. General principles for working with source code

### Locate before you read

The universal pattern, whether the target is a local file, a CI log, or a diff:

1. **Scope** — how big is this, how many files? (`--name-only`, `wc -l`, `Glob`)
2. **Map** — where are the interesting parts? (`grep -n`, `rg`, symbol search)
3. **Read a window** — pull a bounded slice around each coordinate.
4. **Widen only if the window was too small.**

Dumping a whole file "to be safe" is the thing to avoid. It costs context, buries
signal, and makes the next reasoning step harder.

### Choose the tool by where the data lives

| Data | Right tool here | Why |
|---|---|---|
| Local files in the repo | `Grep`/`Glob`/`Read` (or `rg`, `grep -rn`) | Fast, structured, no network |
| CI logs, PR metadata, PR diff, commit patches | `gh` CLI / `gh api` | The only way to reach remote GitHub state; `Read` can't fetch URLs |
| A rendered web page with no API | `WebFetch` | Last resort; returns prose, not structure |

In this session the data was mostly *remote* (Actions logs, PR #3177), so `gh`
did the heavy lifting and local `grep` was only used to check the working tree.
That split is normal for CI triage.

### Always compare against the baseline

A failing assertion is only "introduced by X" if it *passed before X*. Step 6–7
(confirm `main` produces the expected string) is not optional — without it, you
might blame a PR for a flake or a pre-existing break. Concretely: grep the base
branch for the failing string. If it's there, the PR isn't the cause.

### Pin to a commit, not "the PR"

A PR is a moving target. The run records `head_sha`; resolve findings against that
exact commit, then walk forward through later commits to see if it's already
fixed (step 12–13). This turns "the PR broke it" into "commit `c58e67f8` broke it,
`42c8541d` fixed it, the run predates the fix" — which is actionable.

### Read error output literally

The Go failure spelled out `Expected "<actual>" to contain substring "<expected>"`.
The entire investigation flows from diffing those two strings and asking "what
code produces the extra segment." Don't paraphrase error text — extract the exact
literals and grep for them.

### Keep command output small by default

`head`, `tail`, `grep`, `--jq`, `-n`, explicit line ranges, `select(...)`. Every
command in the log above is capped or filtered. This is a habit, not a
special-case optimization: uncapped output is how an agent's context fills with
noise.

---

## 5. Failure modes I hit (and the fixes)

| What happened | Cause | Fix |
|---|---|---|
| `tail -150` of the failed log showed only `PASS` + CI cleanup, no failure | Go streams results; the `FAIL` summary isn't near the end of the raw log | Search by pattern (`grep -E '--- FAIL\|panic:'`), don't read by position |
| `grep … --include=*.rs` → `zsh: no matches found: --include=*.rs` | zsh expands the unquoted glob and aborts on no match (`nomatch` option) | Quote globs: `--include="*.rs"`, and pass an explicit path (`.`) |
| First diff window used raw offsets `160,320` | Offsets into a stream are fragile | For repeatable slicing, use structural delimiters (`/diff --git/,/^diff --git/`) |

---

## 6. Recommendations for the ADK agent

### Tool inventory to expose

Wrap these as discrete, single-purpose tools with small structured returns.
Don't give the model a raw shell and hope.

**GitHub / CI**
- `get_run(run_id)` → `{branch, head_sha, event, jobs:[{id,name,conclusion}]}`
- `get_failed_log(job_id)` → raw text, but the tool should also return
  `{failed_steps:[...]}` extracted
- `find_in_log(job_id, pattern)` → `grep -nE` equivalent, returns `[{line, text}]`
- `read_log_window(job_id, start, end)` → bounded slice
- `get_pr(pr_number)` → `{title, body, author, head_ref, files:[...], commits:[{sha,headline,date}]}`
- `get_pr_diff(pr_number, path=None)` → if `path` given, return only that file's
  hunk (implement with the `sed '/diff --git a\/{path}/,/^diff --git/p'` trick or
  a proper diff parser); otherwise return `--name-only`
- `get_commit_patch(sha, path_regex=None)` → `gh api … --jq` filtered patch

**Local source**
- `search_repo(pattern, globs, path)` → `rg`/`grep -rn`, always returns
  `[{file, line, text}]`, always capped (e.g. 200 hits) with a `truncated` flag
- `read_file_window(path, start, end)` or `read_symbol(path, symbol)`
- `git_grep(pattern, ref)` → search a specific ref, for baseline comparison
- `blame(path, line_range)` → last commit to touch those lines

### Control-flow the agent should follow

```
1. get_run                → failing job(s), head_sha, branch
2. get_failed_log         → extract failed step
3. find_in_log("--- FAIL|panic|Error:")   → test / failure identity
4. read_log_window around the hit         → exact expected-vs-actual
5. extract literal strings from the assertion
6. search_repo(those strings, current branch)   → where behavior is defined
7. git_grep(the failing string, "origin/main")  → BASELINE CHECK
      - present on main  → not introduced by this PR; stop or investigate flake
      - absent on main   → continue
8. get_pr → body + file list  (test file touched? behavior file touched?)
9. get_pr_diff(pr, path=<behavior file>)   → the mechanism
10. map head_sha to a commit; walk later commits for an existing fix
11. report: {failed_test, assertion_delta, offending_commit, already_fixed_by}
```

### Output hygiene rules to bake into every tool

- Every tool return is **capped** (max lines / max hits) and says so
  (`"truncated": true`).
- Prefer returning **coordinates + a small window** over whole files. Offer a
  `read_file_window` follow-up rather than inlining 500 lines.
- Push filters to the source: `gh api --jq`, `grep --include`, `git grep <ref>` —
  don't fetch-then-filter in the agent.
- Address diffs and logs by **structure** (`diff --git` headers, `=== RUN` /
  `--- FAIL` markers, `@@` hunks), not by absolute offsets, so cached results stay
  valid.
- Quote all globs; pass explicit paths. Assume a strict shell (zsh `nomatch`).
- Keep a `head_sha` in the working state from step 1 and resolve every
  source lookup against a known ref, never against "whatever is checked out."

### What made this particular investigation fast

- Stopping `tail` early and switching to pattern search (step 2→3).
- The baseline check (step 6–7) — it converted a guess into a proof.
- Reading the PR body and file list before the diff — the absence of a test file
  in a behavior-changing PR is itself the diagnosis.
- Slicing the diff per-file by its `diff --git` delimiter instead of scrolling.
- Mapping the run to `head_sha` and noticing the very next commit's message
  ("preserve backend error messages") — the fix was already in the PR.

---

## 7. Command cheat sheet

```bash
# --- CI ---
gh run view <run_id> --repo O/R | head -80
gh run view --job <job_id> --repo O/R --log-failed | grep -nE '--- FAIL|panic:|Error:'
gh run view --job <job_id> --repo O/R --log-failed | sed -n 'START,ENDp'
gh api repos/O/R/actions/runs/<run_id> --jq '.head_sha, .head_branch, .created_at'

# --- PR ---
gh pr view <n> --repo O/R --json title,body,author,headRefName,files,commits
gh pr diff <n> --repo O/R --name-only
gh pr diff <n> --repo O/R | sed -n '/diff --git a\/PATH/,/^diff --git/p'   # one file's hunk
gh pr view <n> --repo O/R --json commits \
  --jq '.commits[] | "\(.oid[0:9]) \(.messageHeadline) \(.committedDate)"'

# --- commit ---
gh api repos/O/R/commits/<sha> --jq '.files[].filename'
gh api repos/O/R/commits/<sha> --jq '.files[] | select(.filename|test("REGEX")) | .patch'

# --- local source ---
grep -rn "STR_A\|STR_B" --include="*.rs" --include="*.go" .     # quote the globs
git grep -n "FAILING STRING" origin/main                        # baseline check
```
