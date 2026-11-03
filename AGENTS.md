# AGENTS.md

Project invariants for coding assistants. Read this before changing anything
under `agents/`, `libs/` or `deploy/`. Read `.agents/skills/repo-review/SKILL.md`
before reviewing a diff.

## What this is

An ADK `Workflow` graph (`agents/e2e-agent/e2e_agent/agent/agent.py`) that investigates a
failed GitHub Actions run: it pins the run to frozen shas, lets Gemini drive
code-navigation tools over the GitHub API, verifies the resulting claim
deterministically, writes the outcome to BigQuery, and pauses for a human.
It is deployed to Agent Runtime on Gemini Enterprise Agent Platform as
`e2e-agent` and runs under an Agent Identity.

A second agent, `sre-coordinator` (`agents/sre-coordinator/`), is the front door over
the BigQuery data agent and e2e-agent. **Each agent is its own deployment**: own
directory, Dockerfile, `pyproject.toml`, `agents-cli-manifest.yaml`, pinned env, IAM
grants and engine. They share only `libs/agent_serving` (the Agent Runtime HTTP
harness). Neither imports the other; the coordinator calls e2e-agent over
`stream_query`. `tests/test_layout.py` enforces this.

There is **no clone and no working tree** - GEAP has neither. Every source read
goes through the GitHub API pinned to a sha.

## Commands

```bash
task setup                  # venv + deps + go grammar check; also writes GITHUB_TOKEN (gh auth token) into .env
task test                   # pytest. Offline: no network, no model, no GCP
task investigate RUN=<id>   # whole graph locally against a real failed run; FEEDBACK="..." resumes a pause
task web                    # ADK dev UI on the e2e-agent graph (web:coordinator for the other)
task deploy:e2e             # idempotent update of e2e-agent on Agent Runtime
task deploy:coordinator     # idempotent update of sre-coordinator, independently
task debug VIEW=trace RUN=<id>        # VIEW=report|trace|evidence|triage|logs
task --list                 # everything else (bq:*, iam:*, invoke, publish:*, provision)
```

`task test` must pass before any commit. It takes ~3s; there is no excuse.

## Invariants

These are not style preferences.

1. **Every wait carries a deadline, including the error and cleanup path.**
   Before calling into a client library, read its default and put the actual
   value in a comment. `QueryJob.result()` defaults to waiting forever;
   `job.cancel()` defaults to a 600s retry deadline with no socket timeout;
   `httpx` defaults to no timeout. `tests/test_bounds.py` enforces this
   mechanically - if it fails, bound the call or add it to `ACCEPTED` with the
   reason it is an exception.

2. **A prompt is not a bound.** Changing `prompts.py` so the model is less
   likely to loop is worth doing and is not a guardrail. If a PR's only
   protection is an instruction, say so and name the mechanical backstop that
   catches it when the instruction loses. Instructions are most diluted exactly
   when the context is longest, which is when the loop is worst.

3. **A counter that is never compared is not a bound.** Incrementing
   `dormancy_retry_count` without comparing it to a maximum protects nothing.
   If you add a budget field, add the comparison in the same change.

4. **Bounds compose - multiply them out.** Statements per call x per-statement
   deadline x application retry x node `RetryConfig` attempts. State the product
   in the PR description.

5. **Model output and CI log bytes are untrusted input.** A sha, path, or symbol
   that came from the model or from a log is validated against something this
   run pinned (`rc.known_shas`, the pinned diff) before it is used or blamed.
   That validation lives in `verifier_linter`, never in the prompt.
   Remote text reaches the model only through `tools/untrusted.py:envelope()`,
   which escapes any `<untrusted_ci_output>` tag inside the text so a log cannot
   close the envelope. Do not hand-build the wrapper in a tool. The envelope plus
   `UNTRUSTED_RULE` lowers how often the model is steered; the linter is what
   stops it mattering.

6. **`verifier_linter` is deterministic and contains no LLM call.** It is the
   only thing that distinguishes a trustworthy report from a fabricated one. Do
   not add a model call to it, and do not weaken a check to make a real citation
   resolve without confirming the negative case still fails.

7. **Never paraphrase literals.** Assertion text, error strings, and test names
   are quoted exactly. This is why `events_compaction_config` is deliberately
   unset in `agent.py` - an LLM summarizer would paraphrase the evidence. If
   context growth becomes a problem, cap tool output; do not summarize it.

8. **A swallowed failure must still be visible.** Warehouse and telemetry
   failures are swallowed so they cannot kill an investigation - that is
   correct, and it is half the work. The other half is that a human learns:
   `audit_notes`, the banner, a span attribute, or a counter metric. A
   `log.warning` nobody alerts on is not visibility.

9. **Every node can run twice.** `ResumabilityConfig(is_resumable=True)` is on.
   So: writes are idempotent (`MERGE` on a key, or delete+insert in one
   script); durable data goes to ADK state or artifacts, never a local variable
   held across a `RequestInput`; `interrupt_id` varies per prompt; any node
   calling `ctx.run_node` is declared `@node(rerun_on_resume=True)`.

10. **ADK rejects two edges sharing a `(from, to)` pair.** Converging exits are
    plain edges, and the distinction travels in the payload (`exit_reason`,
    `audit_passed`), not in a route label.

11. **Bound knobs are pinned in `agents/e2e-agent/deploy/runtime.env`.** A bound
    whose production value is whatever the code happens to default to is not a
    number anyone chose. `deploy/deploy.sh` passes that file to `agents-cli
    deploy`. It is the real production configuration. The coordinator's file is
    `agents/sre-coordinator/deploy/runtime.env`.

12. **A new Google API call needs its IAM grant in the same change.**
    e2e-agent grants live in `agents/e2e-agent/deploy/grant_iam.sh`.
    sre-coordinator grants live in `agents/sre-coordinator/deploy/grant_iam.sh`.
    Match the role to the API.

13. **Tests are offline.** No network, no model, no GCP credentials. Fakes must
    be as strict as the real client - a fake that accepts any keyword proves
    nothing. A new guardrail needs a test that trips it.

## Where things are

```
agents/e2e-agent/        e2e_agent/ (package), Dockerfile, deploy/  - one deployment
agents/sre-coordinator/  sre_coordinator/, Dockerfile, deploy/     - another
libs/agent_serving/      Agent Runtime HTTP harness + OTLP auth, shared
deploy/                  generic scripts: deploy.sh, stage.sh, publish.sh, provision.sh
```

Start from `agents/e2e-agent/e2e_agent/agent/agent.py` (graph) and `nodes.py` (node
bodies, budgets, verifier). `agents/e2e-agent/e2e_agent/github/client.py` is the only
path to source bytes. The coordinator is `agents/sre-coordinator/sre_coordinator/agent.py`. PR review: `.agents/skills/repo-review/SKILL.md` (`/repo-review`), also run
by `.github/workflows/claude-review.yml` on a `/review` comment (`docs/ci-review.md`).

## Before you say you are done

- `task test` passes.
- Any new blocking call has a deadline, and its library default is in a comment.
- Any new failure path is observable by a human, not only by a log reader.
- Any new knob is in `agents/e2e-agent/deploy/runtime.env` or `agents/sre-coordinator/deploy/runtime.env`, or explicitly documented as defaulted.
- The PR description names what is still unbounded and why.
