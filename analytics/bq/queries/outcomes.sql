-- Join agent outcomes to the CI run they investigated.
SELECT
  investigation_id,
  run_id,
  workflow_name,
  ci_conclusion,
  verdict,
  audit_passed,
  exit_reason,
  confidence,
  failed_test,
  one_line_summary
FROM `PROJECT_ID.DATASET_ID.v_outcomes`
ORDER BY ingested_at DESC
LIMIT 50;
