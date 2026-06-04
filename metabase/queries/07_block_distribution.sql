-- Average performance per checklist block (radar/bar). Greeting vs needs vs
-- presentation vs closing vs objections vs CRM. Qualification calls only.
SELECT
    cl.block_name,
    ROUND(AVG(cl.percent_of_max), 1) AS avg_pct_of_max,
    ROUND(AVG(cl.score), 2)          AS avg_points
FROM call_criteria_latest cl
JOIN call_scores_latest cs
    ON cs.call_id = cl.call_id AND cs.is_qualification_call IS NOT FALSE
[[WHERE cl.department_name = {{department_name}}]]
GROUP BY cl.block_name
ORDER BY avg_pct_of_max ASC;
