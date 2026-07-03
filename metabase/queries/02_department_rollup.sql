-- Department roll-up: managers ranked into a single line per department.
SELECT
    COALESCE(department_name, '(отдел не назначен)')                       AS department,
    COUNT(DISTINCT manager_id)                                            AS managers,
    COUNT(*)                                                              AS calls_scored,
    ROUND(AVG(percent), 1)                                               AS avg_percent,
    ROUND(100.0 * COUNT(*) FILTER (WHERE zone = 'risk') / COUNT(*), 1)   AS pct_risk
FROM call_scores_latest
WHERE is_qualification_call IS NOT FALSE   -- exclude non-qualification calls
  AND target_status = 'целевой'            -- score only целевые clients
GROUP BY 1
ORDER BY avg_percent DESC;
