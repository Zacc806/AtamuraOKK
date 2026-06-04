-- Per-manager scorecard: average %, zone mix, target ratio, call count.
-- Source: call_scores_latest (latest score per call). Use as a Metabase question;
-- group/sort in the visualization as needed.
SELECT
    manager_name,
    department_name,
    COUNT(*)                                              AS calls_scored,
    ROUND(AVG(percent), 1)                                AS avg_percent,
    ROUND(AVG(duration_sec) / 60.0, 1)                    AS avg_minutes,
    COUNT(*) FILTER (WHERE zone = 'strong')               AS strong,
    COUNT(*) FILTER (WHERE zone = 'normal')               AS normal,
    COUNT(*) FILTER (WHERE zone = 'borderline')           AS borderline,
    COUNT(*) FILTER (WHERE zone = 'risk')                 AS risk,
    COUNT(*) FILTER (WHERE target_status = 'целевой')     AS target,
    COUNT(*) FILTER (WHERE target_status = 'нецелевой')   AS non_target
FROM call_scores_latest
WHERE manager_name IS NOT NULL
  AND is_qualification_call IS NOT FALSE   -- exclude reminders/vendor/internal/etc.
GROUP BY manager_name, department_name
ORDER BY avg_percent DESC;
