-- Per-criterion breakdown for one call (the filled checklist), with the auditor-
-- style justification and the evidence quote. Pair with 09 on the drill-down page.
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
WHERE call_id = {{call_id}}
ORDER BY criterion_id;
