"""Filter sub-90s non-conversations out of the companion/reporting views.

Legacy ingestion stored (and scored) calls shorter than ``ingest_min_duration_sec``
(15-89s "звонок не состоялся" non-conversations). Those rows still carry a score,
so they leak into ``call_scores_latest`` / ``call_criteria_latest`` and drag the
companion's averages. Add a ``duration_sec >= 90`` filter to both views so a short
call's score stays in history but never shows up in the read contract. Mirrors the
ingestion-scan gate and the new ``_apply_scope`` ``too_short`` guard.

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-06-09 02:00:00.000000

"""

from alembic import op

revision = "a7b8c9d0e1f2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def _scores_view(where: str) -> str:
    return f"""
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
LEFT JOIN departments d ON d.id = m.department_id{where};
"""


def _criteria_view(where: str) -> str:
    return f"""
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
CROSS JOIN LATERAL jsonb_array_elements(l.criteria->'per_criterion') AS cr{where};
"""


# duration gate; the leading newline keeps the generated SQL readable.
_FILTER = "\nWHERE c.duration_sec >= 90"


def upgrade() -> None:
    """Recreate both views with the sub-90s filter."""
    op.execute(_scores_view(_FILTER))
    op.execute(_criteria_view(_FILTER))


def downgrade() -> None:
    """Recreate both views without the filter (legacy short calls reappear)."""
    op.execute(_scores_view(""))
    op.execute(_criteria_view(""))
