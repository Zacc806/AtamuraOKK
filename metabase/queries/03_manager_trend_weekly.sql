-- Weekly trend of average score per manager (line/area chart).
-- Optional Metabase filter: {{department_name}} on department_name.
SELECT
    DATE_TRUNC('week', started_at)::date AS week,
    manager_name,
    department_name,
    COUNT(*)                             AS calls,
    ROUND(AVG(percent), 1)               AS avg_percent
FROM call_scores_latest
WHERE manager_name IS NOT NULL
  [[AND department_name = {{department_name}}]]
GROUP BY 1, 2, 3
ORDER BY 1, 2;
