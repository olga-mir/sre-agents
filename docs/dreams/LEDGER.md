# Dream ledger

Memory for the dreaming skill. The procedure is
`.agents/skills/dreaming/SKILL.md`. The watchlist is
[`sources.md`](./sources.md). This file is what the next dream reads.

It is tracked in git on purpose. The skill is the same in every checkout;
the decisions are not. A copy of the skill must not carry this history with
it, and an untracked note would be invisible to the next machine and the
next agent. Commit an entry when you want that memory to travel. The skill
does not commit by itself.

Newest entry first. Never rewrite an old entry. A change of mind is a new
entry that names the earlier date.

Coverage lines are how far a source was actually read. The next dream starts
after the newest successful line for that source. A `failed` line does not
move the source forward. Decisions accumulate: every entry counts, so a
declined idea is not proposed again as if it were new.

No entry below means this repo has not been dreamt yet.
