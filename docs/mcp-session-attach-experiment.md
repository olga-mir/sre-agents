# Experiment: an AI coding agent attaches to a paused e2e-agent session over MCP

Date: 2026-11-14. Run under test: `agent-substrate/substrate` run `43493251346`
(PR 3349). Driver: Claude Code with the `e2e-session` MCP server
(`deploy/session_mcp.py`). Engine: `e2e-agent` on Agent Runtime
(`reasoningEngines/980283060761133056`).

## Question

Can a coding agent, given only `(repo, run_id)`, read a paused e2e-agent
investigation, judge the finding, and answer the pause with a `dig_deeper`
focus it wrote itself?

## Result

Yes. Every step of read, judge and answer went through the MCP tools, and the
engine acted on the focus. Two caveats came out of it, one fixed here and one
open (see Findings).

## What happened, with evidence

### 1. Looking for the "latest session" with no coordinates

The MCP tools need `repo` and `run_id`; there is no list tool. I took the newest
row of `task bq:query Q=outcomes` (run `43530986245`, repo from the Taskfile
default) and called status first, as the server instructions require:

```
mcp__e2e-session__session_status(repo="agent-substrate/substrate", run_id="43530986245")
-> {"state":"done","session_id":"6646627607578148864","report":"","interrupt_id":null}
```

`done` means no pause. I did not call `session_answer` on it; there was nothing
to answer and no human decision to relay.

### 2. Starting a new investigation

The MCP server cannot start runs. `task invoke RUN=43493251346` did start one,
but its stream was cut by the runtime and the session was invisible to the MCP
server:

```
stream closed after events: Failed to parse response as JSON. ...
mcp__e2e-session__session_status(..., run_id="43493251346")
-> {"state":"not_found","session_id":null}
```

Cause: `deploy/invoke_agent.py` filed the session under the hardcoded
`user_id="task-invoke-agent"`. The engine enforces `user_id` on every session
call and the MCP server derives it with `e2e_edge.user_id_for(repo, run_id)`
(`sre-coordinator:<repo>:<run_id>`), so it looked in a different namespace.
Fixed in this change (see below). As a workaround I started the run with the
shared helper, which uses the right user_id:

```python
r = e2e_edge.start(m._engine(), user_id=e2e_edge.user_id_for(repo, run), repo=repo, run_id=run)
# -> {"status": "needs_input", "report": "...", ...}
```

### 3. Reading the pause

```
mcp__e2e-session__session_status(repo="agent-substrate/substrate", run_id="43493251346")
-> {"state":"waiting","session_id":"5504050308365418496",
    "interrupt_id":"deep_dive_request_0",
    "report":"**Egress credential injection token exchange tests fail DNS lookup because
      egress policy rules only whitelist stsHost and omit stsHost.cluster.local.**
      verdict: INTRODUCED_BY_COMMIT   confidence: 0.95
      failed test: TestActorEgressCredentialInjection/token_exchange/https
      ... actual: '... Get \"https://sts.lcxw-871.svc/echoheaders\": dial tcp:
      lookup sts.lcxw-871.svc on 169.254.17.1:53: no such host' ..."}
```

### 4. Judging the finding and writing the focus

The report's mechanism did not fit its own evidence: it says `stsHost` is
whitelisted and only `stsHost+".cluster.local"` is missing, but the failing
lookup is for the bare `sts.lcxw-871.svc`, i.e. `stsHost`. I wrote a focus that
named the contradiction and three things to check (how `stsHost` and its DNS
record are created compared with `echoHost`, how the gateway and guest resolve
rule names, whether the echo subtests pass in the same job) and asked for cited
lines before blaming the commit.

### 5. Answering through MCP

```
mcp__e2e-session__session_answer(
  repo="agent-substrate/substrate", run_id="43493251346", decision="dig_deeper",
  focus="The claim does not match the error. The failing lookup is for
         'sts.lcxw-871.svc', which is stsHost itself ... Verify the cause with
         cited lines before blaming the commit.")
-> {"state":"started","session_id":"5504050308365418496","interrupt_id":"deep_dive_request_0"}

mcp__e2e-session__session_status(...)
-> {"state":"running","elapsed_s":3,"session_id":"5504050308365418496"}
-> {"state":"running","elapsed_s":7,"session_id":"5504050308365418496"}
```

Later, in a new turn:

```
mcp__e2e-session__session_status(...)
-> {"state":"done","interrupt_id":null,"session_id":"5504050308365418496",
    "report":"**My Thought Process** ... I'll go straight to the log file and find the
      entry around line 2570 ... examining egresscredinject_test.go ...
      stsServer and TestActorEgressCredentialInjection ..."}
```

The engine consumed the focus: the pass went to the log around line 2570,
`egresscredinject_test.go`, `stsServer` and `createAndResumeActor`, the things
the focus named.

## Findings

1. **The attach flow works end to end.** Status, judgement, answer and poll all
   ran through `session_status` / `session_answer`; no engine credentials or
   session ids were handled by hand.
2. **Fixed: `task invoke` and the MCP server disagreed on `user_id`.**
   `deploy/invoke_agent.py` now uses `e2e_edge.user_id_for(repo, run_id)`, with a
   test (`test_session_user_id_matches_the_one_session_mcp_looks_up`) that fails
   on the old hardcoded value.
3. **Open: the dig_deeper pass ended `done` with a thinking-text report and no
   pause.** The final report is model reasoning ("**My Thought Process** ...")
   rather than a finding plus a new `finish`/`dig_deeper` question, and it has no
   verdict. First-pass reports also began with leaked reasoning in the raw stream.
   Not investigated here; the person owning the run was expecting a new pause
   and did not get one. Treat the second pass's conclusion as missing, not as
   "the first finding stands".
4. **Open: no tool to start or list sessions.** The MCP server only attaches.

## Agent experience

What worked:
- The server instruction ("call session_status first, show the report verbatim,
  only answer with what the human chose") is a good guard. It kept me from
  answering a `done` session and made the "who decides" boundary explicit.
- The states (`waiting`, `running`, `done`, `not_found`, `answer_failed`) are
  enough to act on without reading code. `interrupt_id` and `session_id` in the
  response let me confirm I was on the same pause I read.
- Reports are self-contained, so I could critique the finding from the text alone.
  That critique (mechanism contradicts the error string) is the kind of judgement
  an agent adds on top of a human's `finish`.

What got in the way:
- **No discovery.** I had to guess coordinates from BigQuery and a Taskfile
  default. A `list_sessions(waiting_only=True)` tool would answer "latest
  paused session" directly.
- **`not_found` is ambiguous.** It meant "wrong user_id" here, which looks the
  same as "never ran". A hint with the user_id searched would shorten diagnosis.
- **Polling is hard to do from an agent.** `session_answer` returns
  immediately and `session_status` does not block, while the pass takes minutes
  and the harness blocks foreground `sleep`. I could not wait for the result in
  the same turn without a scheduler. A `wait_s` parameter on `session_status`,
  bounded by the server, would fix this (it must carry a deadline, per
  AGENTS.md invariant 1).
- **Process-bound background run.** The pass lives in a thread of the MCP server
  process, so closing the host cancels it. An agent cannot tell this from
  `running`.
- **`done` conflates "closed" and "second pass ended without a new pause".**

Rating the approach: good for human-in-the-loop review where the agent supplies
the critique and the human, or the agent acting on an explicit instruction,
supplies the decision. Not yet good for unattended loops until it has discovery,
a bounded wait, and a clear terminal state after `dig_deeper`.
