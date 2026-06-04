-- Pipeline coverage funnel across ALL ingested calls (not just scored), so heads
-- can see how many analyzable calls are still pending vs skipped/parked.
SELECT
    status,
    COUNT(*)                                                AS calls,
    COUNT(*) FILTER (WHERE analyzable)                      AS analyzable,
    COUNT(*) FILTER (WHERE skip_reason IS NOT NULL)         AS skipped,
    COUNT(*) FILTER (WHERE language = 'kk')                 AS kazakh
FROM calls
GROUP BY status
ORDER BY
    ARRAY_POSITION(
        ARRAY['NEW','DOWNLOADED','TRANSCRIBED','SCORED','PENDING_KK','SKIPPED','FAILED','PUSHED'],
        status
    );
