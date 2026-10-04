-- Warehouse for CI-agent eval. Substituted by Taskfile:
--   PROJECT_ID  e.g. your-gcp-project-id
--   DATASET_ID  e.g. ci_agent_eval
-- Location is set by `bq mk --dataset`, not here.

CREATE SCHEMA IF NOT EXISTS `PROJECT_ID.DATASET_ID`
OPTIONS (
  description = "CI/E2E investigation agent: GitHub run inputs, agent outputs, eval outcomes",
  location = "us-central1"
);

-- ---- inputs: GitHub Actions ----------------------------------------------

CREATE TABLE IF NOT EXISTS `PROJECT_ID.DATASET_ID.github_runs` (
  repo STRING NOT NULL,
  run_id STRING NOT NULL,
  workflow_id INT64,
  workflow_name STRING,
  workflow_path STRING,
  event STRING,
  status STRING,
  conclusion STRING,
  run_attempt INT64,
  head_branch STRING,
  head_sha STRING,
  head_repo STRING,
  actor STRING,
  display_title STRING,
  html_url STRING,
  created_at TIMESTAMP,
  updated_at TIMESTAMP,
  run_started_at TIMESTAMP,
  ingested_at TIMESTAMP
)
PARTITION BY DATE(created_at)
CLUSTER BY repo, conclusion, workflow_name
OPTIONS (description = "One GitHub Actions workflow run. Control population includes successes.");

CREATE TABLE IF NOT EXISTS `PROJECT_ID.DATASET_ID.github_jobs` (
  repo STRING NOT NULL,
  run_id STRING NOT NULL,
  job_id STRING NOT NULL,
  job_name STRING,
  status STRING,
  conclusion STRING,
  started_at TIMESTAMP,
  completed_at TIMESTAMP,
  duration_s FLOAT64,
  runner_name STRING,
  step_count INT64,
  failed_steps ARRAY<STRING>,
  ingested_at TIMESTAMP
)
PARTITION BY DATE(started_at)
CLUSTER BY repo, run_id, conclusion
OPTIONS (description = "Jobs on a GitHub Actions run. Failed_steps is empty on success.");

CREATE TABLE IF NOT EXISTS `PROJECT_ID.DATASET_ID._stg_github_runs`
LIKE `PROJECT_ID.DATASET_ID.github_runs`;

CREATE TABLE IF NOT EXISTS `PROJECT_ID.DATASET_ID._stg_github_jobs`
LIKE `PROJECT_ID.DATASET_ID.github_jobs`;

-- ---- outputs: agent invocation + finding ---------------------------------

CREATE TABLE IF NOT EXISTS `PROJECT_ID.DATASET_ID.investigations` (
  investigation_id STRING NOT NULL,
  repo STRING NOT NULL,
  run_id STRING NOT NULL,
  session_id STRING,
  trigger_source STRING,
  paused BOOL,
  source STRING,
  dump_path STRING,
  ingested_at TIMESTAMP
)
CLUSTER BY repo, run_id
OPTIONS (description = "One agent session against one GitHub run.");

CREATE TABLE IF NOT EXISTS `PROJECT_ID.DATASET_ID.findings` (
  investigation_id STRING NOT NULL,
  failure_id STRING NOT NULL,
  verdict STRING,
  failure_type STRING,
  failed_test STRING,
  failed_job_names ARRAY<STRING>,
  confidence FLOAT64,
  audit_passed BOOL,
  exit_reason STRING,
  one_line_summary STRING,
  assertion_expected STRING,
  assertion_actual STRING,
  offending_sha STRING,
  offending_paths ARRAY<STRING>,
  offending_symbols ARRAY<STRING>,
  baseline_search_ok BOOL,
  baseline_present_on_base BOOL,
  baseline_present_on_head BOOL,
  baseline_supports_introduction BOOL,
  flake_signal STRING,
  reasoning STRING,
  audit_notes ARRAY<STRING>,
  budget_max_turns INT64,
  budget_current_turn INT64,
  evidence_count INT64,
  ingested_at TIMESTAMP
)
CLUSTER BY investigation_id, verdict
OPTIONS (description = "Typed CodeFinding + InvestigationSummary outcome per failure_id.");

-- Operational traces stay in the session / Cloud Trace. This table is only
-- the files a code-navigation tool addressed, so many investigations can be
-- compared by path.
DROP VIEW IF EXISTS `PROJECT_ID.DATASET_ID.v_tool_usage`;
DROP TABLE IF EXISTS `PROJECT_ID.DATASET_ID.tool_calls`;
DROP TABLE IF EXISTS `PROJECT_ID.DATASET_ID._stg_tool_calls`;

CREATE TABLE IF NOT EXISTS `PROJECT_ID.DATASET_ID.path_hops` (
  investigation_id STRING NOT NULL,
  seq INT64 NOT NULL,
  tool_name STRING NOT NULL,
  path STRING NOT NULL,
  sha STRING,
  symbol STRING,
  ingested_at TIMESTAMP
)
CLUSTER BY path, investigation_id
OPTIONS (description = "Source files a code-navigation tool addressed, in order. Aggregate with v_path_reach.");

-- Coarse-grained MERGE on these tables has failed with backendError after
-- ~12 minutes of internal retry (a few slot-seconds of real work). Fine-grained
-- DML is the enrolled path for a one-row MERGE. CREATE TABLE IF NOT EXISTS
-- does not update an existing table, so ALTER is what fixes a live dataset.
-- path_hops writes run inside a multi-statement transaction, which BigQuery
-- will not run as fine-grained DML; the option is still set so a later
-- single-statement write can use it.
ALTER TABLE `PROJECT_ID.DATASET_ID.investigations`
SET OPTIONS (enable_fine_grained_mutations = TRUE);
ALTER TABLE `PROJECT_ID.DATASET_ID.findings`
SET OPTIONS (enable_fine_grained_mutations = TRUE);
ALTER TABLE `PROJECT_ID.DATASET_ID.path_hops`
SET OPTIONS (enable_fine_grained_mutations = TRUE);

CREATE TABLE IF NOT EXISTS `PROJECT_ID.DATASET_ID._stg_investigations`
LIKE `PROJECT_ID.DATASET_ID.investigations`;

CREATE TABLE IF NOT EXISTS `PROJECT_ID.DATASET_ID._stg_findings`
LIKE `PROJECT_ID.DATASET_ID.findings`;

CREATE TABLE IF NOT EXISTS `PROJECT_ID.DATASET_ID._stg_path_hops`
LIKE `PROJECT_ID.DATASET_ID.path_hops`;

-- ---- analytics views -----------------------------------------------------

CREATE OR REPLACE VIEW `PROJECT_ID.DATASET_ID.v_ci_health` AS
SELECT
  DATE(created_at) AS day,
  workflow_name,
  conclusion,
  COUNT(*) AS runs,
  COUNTIF(conclusion = "success") AS successes,
  COUNTIF(conclusion = "failure") AS failures
FROM `PROJECT_ID.DATASET_ID.github_runs`
GROUP BY 1, 2, 3;

CREATE OR REPLACE VIEW `PROJECT_ID.DATASET_ID.v_outcomes` AS
SELECT
  i.investigation_id,
  i.repo,
  i.run_id,
  i.trigger_source,
  i.source,
  r.workflow_name,
  r.conclusion AS ci_conclusion,
  r.head_sha,
  r.display_title,
  f.failure_id,
  f.verdict,
  f.failure_type,
  f.failed_test,
  f.confidence,
  f.audit_passed,
  f.exit_reason,
  f.baseline_supports_introduction,
  f.one_line_summary,
  i.ingested_at
FROM `PROJECT_ID.DATASET_ID.investigations` i
LEFT JOIN `PROJECT_ID.DATASET_ID.github_runs` r
  USING (repo, run_id)
LEFT JOIN `PROJECT_ID.DATASET_ID.findings` f
  USING (investigation_id);

CREATE OR REPLACE VIEW `PROJECT_ID.DATASET_ID.v_verdict_rates` AS
SELECT
  verdict,
  COUNT(*) AS n,
  AVG(confidence) AS avg_confidence,
  COUNTIF(audit_passed) / COUNT(*) AS audit_pass_rate,
  COUNTIF(baseline_supports_introduction) AS introduction_with_baseline
FROM `PROJECT_ID.DATASET_ID.findings`
GROUP BY verdict;

-- Files many investigations reach. investigations/failed_tests are the
-- diagnostic (shared file behind many e2e failures); hops is how often the
-- file was actually addressed, including repeat reads inside one run.
-- DISTINCT keeps a multi-finding investigation from inflating either count.
CREATE OR REPLACE VIEW `PROJECT_ID.DATASET_ID.v_path_reach` AS
SELECT
  i.repo,
  h.path,
  COUNT(DISTINCT h.investigation_id) AS investigations,
  COUNT(DISTINCT NULLIF(f.failed_test, '')) AS failed_tests,
  COUNT(DISTINCT FORMAT('%s:%d', h.investigation_id, h.seq)) AS hops,
  ARRAY_AGG(DISTINCT NULLIF(f.failed_test, '') IGNORE NULLS LIMIT 8) AS sample_tests
FROM `PROJECT_ID.DATASET_ID.path_hops` h
JOIN `PROJECT_ID.DATASET_ID.investigations` i
  USING (investigation_id)
LEFT JOIN `PROJECT_ID.DATASET_ID.findings` f
  USING (investigation_id)
GROUP BY i.repo, h.path;
