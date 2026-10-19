# Code review guide

For `src/e2e_agent` and `deploy/`: an ADK `Workflow` graph that runs on Agent
Runtime, drives Gemini with model-chosen tool calls, reads GitHub, writes
BigQuery and GCS, and pauses for a human. That shape decides what a review here
has to look for, which is not what a review of a request/response service looks
for. Three properties make it different:

1. **The loop count is not in the diff.** How many times a tool runs is decided
   by a model at runtime, and how many times an RPC runs is decided by a
   library default. Reading the code tells you neither.
2. **Most failures do not raise.** A poll that never returns, a swallowed
   warehouse error, a model that answers confidently from nothing - all of
   these look like success to the caller and to the tests.
3. **The inputs are hostile by construction.** CI log bytes and model output are
   attacker-influenced text that this code turns into shas, paths, and SQL
   parameters.

Use this on a diff. Order the passes as written: the grep sweep is cheap and
catches the class of defect that is an *absence*, which reading cannot.

---

## Pass 0 - the sweep (always, before reading)

```bash
# A wait with no deadline. .result(), .join(), .get(), requests/httpx without timeout=
rg -n '\.result\(\s*\)|\.join\(\s*\)|\.wait\(\s*\)|Client\(\)\s*$' src/ deploy/
rg -n 'httpx\.(get|post|put)\(|requests\.' src/ deploy/ | rg -v 'timeout'
# Loops with no counter, and fan-out sized by someone else's list
rg -n 'while True|while not |for attempt in range' src/
rg -n 'asyncio\.gather|to_thread|Semaphore' src/
# Config that only exists in a deploy script, never read back by the code
rg -n 'os\.environ' src/ | sed 's/.*environ[.get]*(\["\x27]\?\([A-Z_]*\).*/\1/' | sort -u
rg -n '"[A-Z_]+":' deploy/deploy_agent.py
```

Every hit is a question, not a finding. The finding is a hit whose answer is "I
assumed the default was fine" - and the review output for it is the default's
actual value, cited, in a comment next to the call.

## Pass 1 - liveness: every wait, including the cleanup path

Ask of each blocking call: *what is the longest this can take, and who decided
that number?* An answer that names no number is a finding.

- **Library defaults are part of your diff.** `QueryJob.result()` with no
  argument is `while not is_job_done(): pass` with a 40-minute job restart
  behind it. `job.cancel()` is `retry=DEFAULT_RETRY` (10-minute deadline, retries
  `backendError`) and `timeout=None` (no socket timeout). `httpx.AsyncClient`
  with no `timeout` never gives up. `genai`'s `Gemini(...)` has no HTTP timeout
  unless you pass `http_options`. Open the library and read the default; put the
  value in a comment so the next reader does not have to.
- **The error path needs the same bounds as the happy path.** A timeout handler
  that calls an un-bounded cleanup RPC has moved the hang, not removed it. This
  is the most common way a liveness fix fails review.
- **Bounds must compose.** Multiply them out loud: _n_ statements x per-statement
  deadline x application retries x node `RetryConfig` attempts. `writer.py:86`
  (3 attempts) over three `_wait` calls at 30s is 270s, not 90s.
- **A prompt is not a bound.** "Do not retry that path" in `prompts.py` changes
  a probability. It is worth doing, and it is not a guardrail. If the only thing
  stopping a loop is an instruction, say so in the PR description and name the
  mechanical backstop that catches it when the instruction loses.
- **Cancellation has to reach the work.** `asyncio.to_thread` (`nodes.py:440`)
  survives workflow cancellation. Anything inside it needs its own internal
  deadline, because nothing outside can interrupt it.
- **Whose clock is authoritative?** A client deadline equal to a server deadline
  is a race with no winner. Make the server's smaller so the client observes a
  real error instead of a local timeout.

## Pass 2 - who decides how many times

Separate the three loop owners and bound each one; a bound on one does not cover
the others.

| Owner | Example here | Bound that applies |
|---|---|---|
| Model | tool rounds inside one `ctx.run_node`; `outline_file` retries | `ADK_MAX_LLM_CALLS`, an `after_tool_callback` counter |
| Library | `jobs.get`/`getQueryResults`, `DEFAULT_RETRY`, genai `attempts` | explicit `timeout=` / `retry=` / `job_retry=` |
| Human / graph topology | `dormancy_gate -> invalid_feedback -> dormancy_gate` | a cap compared against, not just incremented |

Specific traps in this repo's shape:

- **A counter that is incremented and never compared is not a bound.**
  `dormancy_retry_count` (`nodes.py:513`, `nodes.py:534`) and
  `budget["token_limit"]` are both written and never read as a limit.
- **`max_turns` counts node invocations, not model calls.** One turn can chain
  dozens of Gemini round trips. A per-turn bound and a per-invocation bound are
  different instruments; check which one the PR actually added.
- **Invocation-wide ceilings are shared budget.** `ADK_MAX_LLM_CALLS` spent by
  `investigator_loop` is not available to `targeted_deep_dive`, so a runaway in
  one node silently disables a later node. If a node must be able to run, it
  needs a reserve.
- **Fan-out sized by an upstream list.** `asyncio.gather` over
  `targets` (`code_tools.py:285`) caps concurrency at 6 and the target count at
  nothing; `_paged` (`github/client.py:204`) caps page size and not page count;
  `rc.failed_jobs` has no cap and `failure_router` downloads a full log per
  entry. Concurrency limits protect the remote service. They do not bound your
  own latency or payload size.

## Pass 3 - does a failure become visible

"Swallowed so the warehouse cannot take down the graph" is the right call and
only half the work. For every `except: log.warning` ask: **which human learns,
and how?**

- A return value that no caller reads is not a report. `record_outcome`
  (`nodes.py:440`) discards `{"ok": false, "error": ...}`, so a permanently
  dropped BigQuery row is invisible in the event stream, in the report, and in
  state.
- Degradations the user should see belong on the artifact the user reads -
  `audit_notes` and the banner, not only the log.
- A new timeout converts "slow but eventually correct" into "fast and silently
  lost". That trade is usually right; it is only complete when the loss is
  counted somewhere. Prefer a span attribute or a counter metric over a log
  line, because an alert can be built on the first two.
- Reviewing a post-mortem fix: ask what would have told you within a minute. If
  the answer is "read the trace", the fix is missing its detector.

## Pass 4 - the trust boundary

Model output and CI log bytes are untrusted input that this code turns into
identifiers.

- **A sha or path read out of model output must be checked against a set this
  run pinned** (`verifier_linter` (c), `rc.known_shas`). New fields on
  `CodeFinding` that name code locations need the same check, and the check
  belongs in the deterministic verifier - never in the prompt.
- **Verification must be referential *and* evidential.** "The artifact exists"
  and "the artifact contains the quoted literal" are different claims. A change
  that makes resolution more permissive - `evidence_candidates` now tries
  several store names - widens what counts as resolved; confirm the negative
  test still fails.
- **Do not let the verifier learn the model's vocabulary.** Every alias added to
  make a real citation resolve also makes a fabricated one more likely to
  resolve. The fix for a model that invents filenames is usually to stop asking
  it for a filename.
- **Range and type checks on model numbers**, with the raw value preserved. The
  confidence clamp at `nodes.py:287` is the pattern: clamp, and say you
  clamped.
- **Values that reach SQL or a store key** go through parameters and through
  `evidence_filename`-style folding. Check that a new field did neither by
  accident.

## Pass 5 - state, resumability, idempotency

`ResumabilityConfig(is_resumable=True)` means every node can run twice.

- In-memory state does not survive a resume; only ADK state and artifacts do. A
  new node holding bytes in a local variable across a `RequestInput` is a bug.
- `interrupt_id` must vary per prompt (`deep_dive_request_{retry_count}`). A
  reused id wedges the loop on resume.
- `save_artifact` versions on every write, so read-before-write is required, and
  a put in a retried node is a new version unless guarded.
- A node that can run twice must write idempotently: `MERGE` on a key, or
  delete-then-insert in one script. Check the new write's key, not that it has
  one.
- `@node(rerun_on_resume=True)` is required for any node calling `ctx.run_node`.
  Adding dynamic scheduling to an existing node and forgetting this is silent.

## Pass 6 - cost, quota, and context growth

These behave like correctness properties here, because they end the run.

- Input tokens grow with every tool response in a turn. `events_compaction_config`
  is deliberately off; that decision has a cost, and a diff that adds tool
  responses or lengthens them pays it. Prompt instructions added late in a long
  turn are the most diluted.
- Gemini runs on Dynamic Shared Quota: 429 is a normal operating condition, not
  abuse. New model calls need a retry policy and a routed exit, not an
  exception.
- A tool that returns bytes needs a cap on bytes AND the caller needs a cap on
  calls. The existing caps (`MAX_HITS`, `WINDOW_CAP`, `SYMBOL_CAP`) are the
  first only.
- Payload growth downstream: `hops_json` is one string parameter built from
  every code-navigation call in the session. A runaway loop produces both the
  incident and the oversized write that fails to record it.

## Pass 7 - deployment, identity, and config that lives outside the code

`deploy/deploy_agent.py` `env_vars` is the real production configuration.

- Every tuning knob read by `src/` should be set there or documented as
  defaulted. Every key set there should be read by something. Both drifts are
  findings.
- A default in code and a value in `env_vars` that disagree means local and
  deployed behave differently - say which one the tests exercise.
- `os.environ.get(...)` at module scope freezes the value at import. Fine for
  deployment; wrong for anything a test or a runtime caller wants to change.
- New Google API calls need the IAM grant in the same change:
  `deploy/grant_agent_identity_iam.sh` for e2e-agent,
  `deploy/grant_coordinator_iam.sh` for sre-coordinator. The role has to
  match the error: `401 Invalid Credentials` is a token problem, `403` is a
  binding problem. Adding a binding
  for a 401 does not fix it.
- An integration that is enabled, failing, and retrying in the background costs
  on every model call and delivers nothing. Turn it off until it works.

## Pass 8 - the test that would have failed

- The test must fail against the pre-change code. A fake whose method signature
  was widened to accept the new keyword (`def result(self, timeout=None, **kwargs)`)
  proves the keyword is passed, not that anything is bounded.
- Assert the bound, not the happy path: the deadline value, the cancel count,
  the attempt count, the state after exhaustion.
- Fakes must be as strict as the real client. A `cancel()` that takes no
  keywords cannot catch a `cancel()` that needs them.
- A guardrail without a test that trips it will be removed by the next person
  who finds it inconvenient.

---

## Severity

1. **Can hold or kill a live investigation** - unbounded wait, unbounded loop,
   cancellation that does not reach the work, a write that can outlive the
   request.
2. **Can produce a confident wrong answer** - trust-boundary gap, verifier
   weakened, untrusted value used as an identifier.
3. **Can lose data or hide a failure** - swallowed error with no counter,
   non-idempotent write in a resumable node, dropped row.
4. **Cost and latency** - unbounded fan-out, context growth, retries that
   multiply.
5. **Drift** - config set and never read, IAM grant missing, comment that no
   longer matches the library.

## Findings this guide should not produce

Naming, formatting, docstring style, type-annotation completeness, "add a test"
with no named case, "consider extracting a helper", or a bound proposed with no
number and no source for the number. If a finding cannot name the input that
triggers it and the observable consequence, it is not a finding.
