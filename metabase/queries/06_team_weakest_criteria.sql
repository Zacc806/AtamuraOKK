-- Team's weakest checklist criteria (avg % of max). Drives "systemic errors" and
-- the training focus in the weekly/monthly reports. Qualification calls only.
SELECT
    cl.criterion_id,
    cl.block_name,
    cl.criterion_text,
    COUNT(*)                             AS scored,
    ROUND(AVG(cl.percent_of_max), 1)     AS avg_pct_of_max,
    ROUND(AVG(cl.score), 2)              AS avg_points,
    MAX(cl.max)                          AS max_points
FROM call_criteria_latest cl
JOIN call_scores_latest cs
    ON cs.call_id = cl.call_id AND cs.is_qualification_call IS NOT FALSE
[[WHERE cl.department_name = {{department_name}}]]
GROUP BY cl.criterion_id, cl.block_name, cl.criterion_text
ORDER BY avg_pct_of_max ASC;
