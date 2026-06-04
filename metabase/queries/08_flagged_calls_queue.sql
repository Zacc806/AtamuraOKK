-- Flagged-calls queue: low score, non-target, negative customer sentiment, or any
-- red flag. The ОКК / РОП work list. Click a row -> open the drill-down dashboard
-- filtered by call_id.
SELECT
    call_id,
    scored_at::date                       AS scored_on,
    manager_name,
    department_name,
    percent,
    zone,
    call_type,
    target_status,
    sentiment_customer,
    COALESCE(JSONB_ARRAY_LENGTH(red_flags), 0) AS n_flags,
    red_flags,
    summary
FROM call_scores_latest
WHERE is_qualification_call IS NOT FALSE   -- manager-performance queue only
  AND (
        zone = 'risk'
     OR target_status = 'нецелевой'
     OR sentiment_customer = 'негативный'
     OR COALESCE(JSONB_ARRAY_LENGTH(red_flags), 0) > 0
      )
  [[AND department_name = {{department_name}}]]
ORDER BY percent ASC, n_flags DESC;
