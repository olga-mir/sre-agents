SELECT
  workflow_name,
  conclusion,
  COUNT(*) AS runs,
  ROUND(AVG(TIMESTAMP_DIFF(updated_at, created_at, SECOND)), 1) AS avg_wall_s
FROM `PROJECT_ID.DATASET_ID.github_runs`
GROUP BY 1, 2
ORDER BY runs DESC;
