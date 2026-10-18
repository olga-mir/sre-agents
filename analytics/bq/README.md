# e2e-agent eval warehouse

BigQuery dataset `ci_agent_eval` in `GOOGLE_CLOUD_PROJECT` (`us-central1`).

It stores three layers that you join later, once there are enough investigations:

| Layer | Tables | What it is |
|---|---|---|
| **Inputs** | `github_runs`, `github_jobs` | What GitHub Actions actually did. Includes **successful** runs as the control population. |
| **Outputs** | `investigations`, `findings`, `path_hops` | What the agent concluded, and which source files it opened. |
| **Outcomes** | view `v_outcomes` | Verdict + verifier (`audit_passed`, `exit_reason`, `baseline_supports_introduction`) joined back to the CI run. |

Evidence **bytes** stay in ADK artifacts / `runs/<id>/evidence/`. This warehouse is structured facts only.

## How the agent talks to it

**Writes (this agent):** a deterministic `record_outcome` FunctionNode after `verifier_linter` and again after `targeted_deep_dive`. It MERGEs `investigations` + `findings` keyed on `(investigation_id, failure_id)` (`session:{adk_session_id}`), and replaces `path_hops` for that investigation from the session's code-tool calls. The SQL is fixed and parameterized — the model never sees BigQuery. A write failure is logged and swallowed so the investigation still reaches the human. The deep-dive write replaces the hop path, so files opened only after the human replies are included.

Disable with `E2E_AGENT_BQ_WRITE=0`. The Agent Runtime SA (and your user, for local `task investigate`) need `roles/bigquery.dataEditor` on this dataset: `task bq:iam MEMBER=user:you@example.com`.

**Reads:** not on this agent. A separate mechanism will query and parse the warehouse. Do not bind the managed BigQuery MCP (`https://bigquery.googleapis.com/mcp`, Agent Registry global `bigquery.googleapis.com`) to `investigator_loop`.

**Not an artifact store, and not an execution trace.** Logs, source windows, tool arguments, and timings stay in artifacts and in the Agent Runtime session / Cloud Trace. `path_hops` keeps only the source files those code tools addressed (`path`, `sha`, `symbol`), so you can see which files many investigations reach.

`v_path_reach` is that comparison: one row per `(repo, path)`, with how many investigations opened it, how many distinct failing tests, and a sample of those tests. A path that many different e2e failures reach is a development priority and a signal about the file itself. `task bq:query Q=path_reach`.

Offline seed (`task bq:seed`, `task bq:seed WHAT=investigations`) still loads GitHub control runs and local `runs/<id>/` dumps. Dump hops use `local:{run_id}`; the graph uses `session:{adk_session_id}`.

## Tasks

```bash
task bq:iam MEMBER=user:you@example.com   # dataset, tables, views, then the grant
task bq:seed                              # collect + MERGE GitHub rows
task bq:seed WHAT=investigations          # load local runs/ dumps
task bq:query Q=ci_health                 # Q=verdict_rates | path_reach | outcomes
```

`task bq:seed` and `task bq:iam` enable the BigQuery API and apply the DDL.
`LIMIT=40 WORKFLOW=pr-workflow CONCLUSION=success` filter the github seed.
`CONCLUSION=failure` collects failed runs the agent actually investigates.
