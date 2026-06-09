"""Reporting view DDL, shared by Alembic and the test harness.

The ``call_scores_latest`` / ``call_criteria_latest`` views flatten the latest
score per call out of JSONB into clean columns. They are the **read contract**
the reporting layer and the companion ``/api/v1`` read API depend on — neither
touches the raw ``scores`` table or the pipeline ``status`` enum directly.

In production these views are owned by the migrations (revisions
``b2c3d4e5f6a7`` + ``c3d4e5f6a7b8``). That migration history is immutable, so
this module simply restates the *current* DDL in one place that both a future
migration and the test fixtures (which build the schema via ``meta.create_all``,
bypassing migrations) can reuse. Keep this in sync with the latest migration.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

CALL_SCORES_LATEST = """
CREATE OR REPLACE VIEW call_scores_latest AS
WITH latest AS (
    SELECT DISTINCT ON (s.call_id) s.*
    FROM scores s
    ORDER BY s.call_id, s.created_at DESC, s.id DESC
)
SELECT
    c.id                                             AS call_id,
    c.bitrix_call_id,
    c.portal_user_id,
    c.manager_id,
    NULLIF(TRIM(CONCAT_WS(' ', m.name, m.last_name)), '') AS manager_name,
    m.bitrix_user_id                                 AS manager_bitrix_user_id,
    m.department_id,
    d.name                                           AS department_name,
    d.bitrix_id                                      AS department_bitrix_id,
    c.direction,
    c.started_at,
    c.duration_sec,
    c.language,
    l.id                                             AS score_id,
    l.rubric_version,
    l.model                                          AS scoring_model,
    l.created_at                                     AS scored_at,
    l.total_score                                    AS percent,
    (l.criteria->>'zone')                            AS zone,
    (l.criteria->>'raw_points')::int                 AS raw_points,
    (l.criteria->>'max_points')::int                 AS max_points,
    (l.criteria->>'target_status')                   AS target_status,
    (l.criteria->>'objections_present')::boolean     AS objections_present,
    (l.sentiment->>'customer')                       AS sentiment_customer,
    (l.sentiment->>'agent')                          AS sentiment_agent,
    l.summary,
    (l.criteria->>'strengths')                       AS strengths,
    (l.criteria->>'growth_zone')                     AS growth_zone,
    (l.criteria->>'training_recommendation')         AS training_recommendation,
    l.flags                                          AS red_flags,
    (l.criteria->>'call_type')                       AS call_type,
    (l.criteria->>'is_qualification_call')::boolean  AS is_qualification_call
FROM latest l
JOIN calls c       ON c.id = l.call_id
LEFT JOIN managers m    ON m.id = c.manager_id
LEFT JOIN departments d ON d.id = m.department_id
-- Hide sub-threshold non-conversations (mirrors ingest_min_duration_sec=90):
-- legacy short calls may carry a score, but they are never shown/reported.
WHERE c.duration_sec >= 90;
"""

CALL_CRITERIA_LATEST = """
CREATE OR REPLACE VIEW call_criteria_latest AS
WITH latest AS (
    SELECT DISTINCT ON (s.call_id) s.*
    FROM scores s
    ORDER BY s.call_id, s.created_at DESC, s.id DESC
)
SELECT
    c.id                                             AS call_id,
    c.manager_id,
    NULLIF(TRIM(CONCAT_WS(' ', m.name, m.last_name)), '') AS manager_name,
    m.department_id,
    d.name                                           AS department_name,
    c.started_at,
    l.rubric_version,
    (cr->>'id')::int                                 AS criterion_id,
    cr->>'block_id'                                  AS block_id,
    cr->>'block_name'                                AS block_name,
    cr->>'text'                                      AS criterion_text,
    (cr->>'score')::numeric                          AS score,
    (cr->>'max')::numeric                            AS max,
    CASE
        WHEN (cr->>'max')::numeric > 0
        THEN ROUND((cr->>'score')::numeric / (cr->>'max')::numeric * 100, 1)
    END                                              AS percent_of_max,
    cr->>'justification'                             AS justification,
    cr->>'evidence'                                  AS evidence,
    cr->>'recommendation'                            AS recommendation
FROM latest l
JOIN calls c       ON c.id = l.call_id
LEFT JOIN managers m    ON m.id = c.manager_id
LEFT JOIN departments d ON d.id = m.department_id
CROSS JOIN LATERAL jsonb_array_elements(l.criteria->'per_criterion') AS cr
-- Mirror call_scores_latest: never surface criteria for sub-threshold calls.
WHERE c.duration_sec >= 90;
"""


async def create_reporting_views(conn: AsyncConnection) -> None:
    """Create (or replace) the reporting views on an async connection.

    Used by the test harness, which builds the schema with ``meta.create_all``
    and therefore never runs the migrations that own these views in production.
    """
    await conn.execute(text(CALL_SCORES_LATEST))
    await conn.execute(text(CALL_CRITERIA_LATEST))
