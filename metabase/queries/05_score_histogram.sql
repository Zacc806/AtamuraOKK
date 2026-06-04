-- Score distribution as 10-point buckets (histogram). Shows the spread instead of
-- just the average, which matters because first-call scores skew low.
SELECT
    (FLOOR(percent / 10) * 10)::int                        AS bucket_start,
    (FLOOR(percent / 10) * 10 + 10)::int                   AS bucket_end,
    COUNT(*)                                               AS calls
FROM call_scores_latest
WHERE percent IS NOT NULL
  AND is_qualification_call IS NOT FALSE
  [[AND department_name = {{department_name}}]]
GROUP BY 1, 2
ORDER BY 1;
