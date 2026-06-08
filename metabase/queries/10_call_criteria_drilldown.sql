-- Per-criterion breakdown for one call (the filled checklist), with the auditor-
-- style justification and the evidence quote. Pair with 09 on the drill-down page.
-- OPTIONAL {{call_id}}: like 09 it falls back to the latest scored call when unset,
-- so the two cards always show the same call and the page never errors on open.
SELECT
    criterion_id,
    block_name,
    criterion_text,
    score,
    max,
    percent_of_max,
    justification,
    evidence
FROM call_criteria_latest
WHERE call_id = (
    SELECT cs.call_id
    FROM call_scores_latest cs
    WHERE 1 = 1
        [[AND cs.call_id = {{call_id}}]]
    ORDER BY cs.scored_at DESC
    LIMIT 1
)
ORDER BY criterion_id;
