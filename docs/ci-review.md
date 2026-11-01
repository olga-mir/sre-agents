# PR review in CI

Comment `/review` on a pull request and Claude reviews it with the [`repo-review`](../.agents/skills/repo-review/SKILL.md) skill, then posts one PR comment. Workflow: [`.github/workflows/claude-review.yml`](../.github/workflows/claude-review.yml).

## One-time setup

### Anthropic Console (needs org admin; an API org, not a Pro chat plan)

Authentication is Workload Identity Federation: GitHub's OIDC token is exchanged for a short-lived Anthropic token, so there is no API key to store.
Reference: [claude-code-action setup](https://github.com/anthropics/claude-code-action/blob/main/docs/setup.md#workload-identity-federation).

1. Console → **Settings → Keys & Access → Workload identity**. [Claude WIF console](https://platform.claude.com/settings/workload-identity-federation) register an issuer: `https://token.actions.githubusercontent.com`, JWKS source `discovery`.
2. **Settings → Service accounts** → create one, add it to the workspace that should be billed.
3. Create a **federation rule** for the SA, matching subject prefix `repo:<user-or-org>/<repo-name>:*`

Collect following IDs:
* organization UUID and the `wrkspc_...`.
* federation rule: `fdrl_...`.
* SA: `svac_...`.

### GitHub

1. **Install the Claude GitHub App** on this repo (<https://github.com/apps/claude>) or run `claude /install-github-app`. Contents, Pull requests and Issues permissions are enough; the workflow token itself is read-only plus PR comments.
2. **Repository secrets** (Settings → Secrets and variables → Actions →
   Secrets). They are not credentials, but this repo is public and variables
   are printed unmasked in run logs; secrets are masked:

   | Secret | Value |
   |---|---|
   | `ANTHROPIC_FEDERATION_RULE_ID` | `fdrl_...` |
   | `ANTHROPIC_ORGANIZATION_ID` | org UUID |
   | `ANTHROPIC_SERVICE_ACCOUNT_ID` | `svac_...` |
   | `ANTHROPIC_WORKSPACE_ID` | `wrkspc_...` |

3. **Do not set** `ANTHROPIC_API_KEY` or an OAuth token in this repo. A static credential takes precedence and silently disables federation.
4. This repo is public. Settings → Actions → General → *Fork pull request workflows*: require approval for all outside collaborators. The workflow already refuses fork PRs and non-owner comments, but that setting covers the other workflows too.

## Using it

Comment `/review` on a PR. A second `/review` on the same PR cancels the first. Re-run it after pushing; it never runs on its own.

## What the reviewer sees

- The PR head, full history, `ripgrep`.
- `AGENTS.md` and the `repo-review` skill. The action restores `.claude/` from the **base** branch, so a PR cannot change the reviewer's own skills or settings; edits to the guide take effect after merge.
- `google-agents-cli` skills (`google-agents-cli-adk-code`, `-deploy`, ...), installed globally on the runner by `agents-cli setup` at the version pinned in the workflow. Global scope is deliberate: a workspace-level install lands in `.claude/` and is deleted by the base-branch restore.
- `https://adk.dev` through `WebFetch`. (TODO to confirm)

## Bounds

Per invariants 1 and 4:

| Bound | Value | Where |
|---|---|---|
| Wall time | 20 min | `timeout-minutes` |
| Model turns | 40 | `--max-turns` |
| Concurrency | 1 per PR, newer cancels older | `concurrency` |
| Writes | one PR comment | `--allowedTools` |

Still unbounded: tokens per turn (long diffs), and `agents-cli setup`'s npm fetch, which is covered only by the 20-minute job limit.

## Built-in `/code-review` vs `repo-review`

Use them for different things.

- **`/code-review`** (built into Claude Code) is a generic correctness pass: logic bugs, plus reuse and simplification. Run it locally before you push.
- **`repo-review`** is this repo's contract: liveness, who owns each loop, swallowed failures, trust boundary, resumability, and an explicit list of findings *not* to produce. It is what CI runs.

CI runs only `repo-review`. Running both would post overlapping comments, and the generic reviewer's simplification findings are on `repo-review`'s do-not-report list.
