MERGE `PROJECT_ID.DATASET_ID.github_runs` T
USING `PROJECT_ID.DATASET_ID._stg_github_runs` S
ON T.repo = S.repo AND T.run_id = S.run_id
WHEN MATCHED THEN UPDATE SET
  workflow_id = S.workflow_id,
  workflow_name = S.workflow_name,
  workflow_path = S.workflow_path,
  event = S.event,
  status = S.status,
  conclusion = S.conclusion,
  run_attempt = S.run_attempt,
  head_branch = S.head_branch,
  head_sha = S.head_sha,
  head_repo = S.head_repo,
  actor = S.actor,
  display_title = S.display_title,
  html_url = S.html_url,
  created_at = S.created_at,
  updated_at = S.updated_at,
  run_started_at = S.run_started_at,
  ingested_at = S.ingested_at
WHEN NOT MATCHED THEN INSERT ROW;

MERGE `PROJECT_ID.DATASET_ID.github_jobs` T
USING `PROJECT_ID.DATASET_ID._stg_github_jobs` S
ON T.repo = S.repo AND T.run_id = S.run_id AND T.job_id = S.job_id
WHEN MATCHED THEN UPDATE SET
  job_name = S.job_name,
  status = S.status,
  conclusion = S.conclusion,
  started_at = S.started_at,
  completed_at = S.completed_at,
  duration_s = S.duration_s,
  runner_name = S.runner_name,
  step_count = S.step_count,
  failed_steps = S.failed_steps,
  ingested_at = S.ingested_at
WHEN NOT MATCHED THEN INSERT ROW;
