-- Files reached by the most investigations. A path many failing tests open
-- is a development priority and, on its own, a signal that the file is involved.
SELECT
  repo,
  path,
  investigations,
  failed_tests,
  hops,
  sample_tests
FROM `PROJECT_ID.DATASET_ID.v_path_reach`
ORDER BY investigations DESC, failed_tests DESC, hops DESC
LIMIT 50;
