-- Score-zone distribution (bar/pie). Ordered strong -> risk.
SELECT
    zone,
    COUNT(*)                                                AS calls,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1)      AS pct
FROM call_scores_latest
WHERE zone IS NOT NULL
  AND is_qualification_call IS NOT FALSE
GROUP BY zone
ORDER BY ARRAY_POSITION(ARRAY['strong', 'normal', 'borderline', 'risk'], zone);
