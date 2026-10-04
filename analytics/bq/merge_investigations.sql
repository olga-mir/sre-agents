MERGE `PROJECT_ID.DATASET_ID.investigations` T
USING `PROJECT_ID.DATASET_ID._stg_investigations` S
ON T.investigation_id = S.investigation_id
WHEN MATCHED THEN UPDATE SET
  repo = S.repo,
  run_id = S.run_id,
  session_id = S.session_id,
  trigger_source = S.trigger_source,
  paused = S.paused,
  source = S.source,
  dump_path = S.dump_path,
  ingested_at = S.ingested_at
WHEN NOT MATCHED THEN INSERT ROW;

MERGE `PROJECT_ID.DATASET_ID.findings` T
USING `PROJECT_ID.DATASET_ID._stg_findings` S
ON T.investigation_id = S.investigation_id AND T.failure_id = S.failure_id
WHEN MATCHED THEN UPDATE SET
  verdict = S.verdict,
  failure_type = S.failure_type,
  failed_test = S.failed_test,
  failed_job_names = S.failed_job_names,
  confidence = S.confidence,
  audit_passed = S.audit_passed,
  exit_reason = S.exit_reason,
  one_line_summary = S.one_line_summary,
  assertion_expected = S.assertion_expected,
  assertion_actual = S.assertion_actual,
  offending_sha = S.offending_sha,
  offending_paths = S.offending_paths,
  offending_symbols = S.offending_symbols,
  baseline_search_ok = S.baseline_search_ok,
  baseline_present_on_base = S.baseline_present_on_base,
  baseline_present_on_head = S.baseline_present_on_head,
  baseline_supports_introduction = S.baseline_supports_introduction,
  flake_signal = S.flake_signal,
  reasoning = S.reasoning,
  audit_notes = S.audit_notes,
  budget_max_turns = S.budget_max_turns,
  budget_current_turn = S.budget_current_turn,
  evidence_count = S.evidence_count,
  ingested_at = S.ingested_at
WHEN NOT MATCHED THEN INSERT ROW;

DELETE FROM `PROJECT_ID.DATASET_ID.path_hops`
WHERE investigation_id IN (
  SELECT DISTINCT investigation_id FROM `PROJECT_ID.DATASET_ID._stg_path_hops`
);

INSERT INTO `PROJECT_ID.DATASET_ID.path_hops`
SELECT * FROM `PROJECT_ID.DATASET_ID._stg_path_hops`;
