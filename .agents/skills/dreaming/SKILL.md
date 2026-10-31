---
name: dreaming
description: >-
  Runs a dreaming pass over this repo's curated ADK, Agent Platform, Gemini
  Enterprise Agent Platform, Google Developers Blog, and discuss.google.dev
  sources. Reports notable changes, insights, and concrete improvements, then
  records decisions in docs/dreams/LEDGER.md. Use only when the user invokes
  /dreaming or asks to dream, run a dream, or catch up on ADK or Agent
  Platform releases for this project. Do not use during ordinary coding,
  debugging, or review.
disable-model-invocation: true
compatibility: >-
  Needs a way to fetch public web pages. No host-specific tools. Cursor,
  Claude Code, and Antigravity all invoke it as /dreaming. Sources and the
  ledger are docs/dreams/ in this repo.
---

# Dreaming

Look at what changed in the ADK and Agent Platform world since the last dream, say what is worth knowing, and propose only the changes that would make this repo a better demo of those ideas. Then remember what was decided.

The procedure is this file. The watchlist and the memory are not. Read both from the repository root:

- `docs/dreams/sources.md` — what to fetch, and what to ignore on each page
- `docs/dreams/LEDGER.md` — when the last dream ran, how far each source was read, and every adopt / decline / defer

A dream that rewrites either file's rules, or that stores the ledger inside this skill directory, will lose the memory the next time the skill is copied. Update the ledger only in the way the ledger's header describes.

## Ground the pass in this repo

Before fetching, read:

1. `AGENTS.md` — proposals that fight an invariant get said as such, not recommended.
2. `docs/README.md` — this is a conference demo of sre-coordinator, e2e-agent, and a BigQuery data agent. It is not a production template. Prefer a proposal that teaches a platform idea the demo does not show yet, or that closes a gap the demo already has.
3. `pyproject.toml` — the `google-adk` and `google-cloud-aiplatform` / `google-cloud-agentplatform` floors. Quote them; do not trust a version remembered from an older dream.
4. The whole ledger. Coverage comes from the newest entry. Decisions come from every entry. A declined idea that shows up again is "already decided", not a new proposal.
5. `git log -20 --oneline` — so a proposal is not something that already landed.

## Window

If the newest ledger entry has a successful coverage line for a source, fetch only what is newer than that line. If that source's newest line says `failed`, walk older entries until a successful line, and start there. Say which date you used.

If a source has never been covered:

- ADK and the Agent Platform SDK: every release at or above the matching pin in `pyproject.toml`, inclusive, so the dream sees what this repo is already sitting on and what it has not bumped to. Name the pin.
- The blog, the GEAP release notes, and both discuss tags: from the publish date of that pinned ADK release. If that date cannot be read, use the last 45 days and say the window is a fallback.

Today's date is the session date. Do not invent a previous dream.

## Fetch

Use whatever fetch or browser this host already has. Do not require a particular vendor tool. `gh` is fine for the two GitHub release pages when it is already authenticated; fetching the page URL is equally fine.

Follow `docs/dreams/sources.md`. Do not add a standing source during a dream. A link the user names in the same message is in scope for this pass only and is not written into `sources.md` unless they ask.

Bounds, so a long page cannot become the whole session:

- On a dated release-notes page, stop at the first entry older than the window.
- For ADK and the Agent Platform SDK, read highlights and breaking changes. Open the full body only when a highlight might matter here.
- Open at most 5 blog posts and 8 discuss threads, newest matching items first.
- If a page is truncated or a fetch fails, record that source as failed. Do not fill the gap from memory.

Quote version numbers, feature names, and breaking-change sentences as the source wrote them. Paraphrase only the "why it matters here" line. A feature name you are not sure you read is left out.

## What to bring back

Write the dream in the conversation. Do not edit the repo in this step.

```markdown
# Dream YYYY-MM-DD

Window: <how the start was chosen>. Previous dream: <date or none>.

## Notable changes
Short, sourced, quoted names. Group by source. Omit items the keep-rules rejected. One line each: what shipped, version or date, link.

## Insights and ideas
Observations that are interesting and not yet a change to this repo. A talk point, a shift in how the platform wants agents built, a trap.

## Proposals
Numbered. For each:
- Why now: source, date, link, quoted name
- Why here: which of sre-coordinator, e2e-agent, the data agent, deploy, or the docs it would change
- Touch: the area of the tree
- Invariants: "fits" or the specific AGENTS.md rule it would press on
- Scope: demo-sized, or interesting but out of scope for this repo

## Already decided
Ledger items that reappeared, with the earlier adopt / decline / defer, quoted.

## Not in this pass
Sources that failed, caps that truncated a page, and off-topic material you skipped.
```

Skip an empty section's body with "nothing in this window" rather than omitting the heading. The next dream compares against these headings.

A proposal is a suggestion. Do not implement it, open a branch, or edit product code in the dream turn. Implementation is a later request.

## Record the dream

The ledger does not move until the user answers. Ask which numbered proposals to adopt, decline, or defer. "Record this dream" with no picks still writes an entry: coverage and noted items move forward, and Decisions says `none`.

When they answer, prepend one entry to `docs/dreams/LEDGER.md`, below the header and above older entries. Never edit an old entry. A change of mind is a new entry that names the date of the old one.

Use this shape so the next dream can find the lines:

```markdown
## YYYY-MM-DD

### Coverage
- adk-python: through vX.Y.Z (YYYY-MM-DD)
- agentplatform: through vX.Y.Z (YYYY-MM-DD)
- geap-release-notes: through YYYY-MM-DD
- developers-blog: through YYYY-MM-DD
- discuss-googler: through YYYY-MM-DD
- discuss-agent-platform: through YYYY-MM-DD

### Noted
- quoted feature or post title — source, date

### Decisions
- adopt: one line. touches: area
- decline: one line. why: one line
- defer: one line. revisit: what would make it worth reopening
- none

### Landed since previous dream
- one line naming the earlier decision and where it shows up now
- nothing new landed
```

A source that failed this pass gets `failed` instead of `through ...`, with the reason in the same bullet. Leave its previous successful line alone.

`Landed since previous dream` is what you observed in the tree and in `git log` at the start of this dream, for items an earlier entry marked `adopt`. Write it only into the new entry.

Keep noted items and decisions to one line. The long form stays in the chat. The ledger is the index.

After writing it, say that the file is the memory and that other machines and other agents see it only once it is committed. Do not commit unless the user asks.
