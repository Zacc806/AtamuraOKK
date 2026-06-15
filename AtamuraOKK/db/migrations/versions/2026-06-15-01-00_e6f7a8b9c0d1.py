"""Surface client_category in the reporting views.

Adds the manager-assigned lead category (A/B/C/X) to ``call_scores_latest`` and
``call_criteria_latest`` so Metabase can break scores down by category — and a
``scored_category`` column (the category scoring actually applied, read from the
score JSONB) so a divergence from the call's current tag is visible. The new
columns are appended at the END of each SELECT (Postgres only lets
``CREATE OR REPLACE VIEW`` add trailing columns); the `duration_sec >= 90` filter
and `DISTINCT ON` latest-score CTE are unchanged.

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-06-15 01:00:00.000000

"""

from alembic import op

revision = "e6f7a8b9c0d1"
down_revision = "d5e6f7a8b9c0"
branch_labels = None
depends_on = None

_FILTER = "\nWHERE c.duration_sec >= 90"


def _scores_view(*, category: bool) -> str:
    cat_cols = (
        """,
    c.client_category                                AS client_category,
    (l.criteria->>'client_category')                 AS scored_category"""
        if category
        else ""
    )
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
    (l.criteria->>'is_qualification_call')::boolean  AS is_qualification_call{cat_cols}
FROM latest l
JOIN calls c       ON c.id = l.call_id
LEFT JOIN managers m    ON m.id = c.manager_id
LEFT JOIN departments d ON d.id = m.department_id{_FILTER};
"""


def _criteria_view(*, category: bool) -> str:
    cat_col = (
        ",\n    c.client_category                                AS client_category"
        if category
        else ""
    )
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
    cr->>'recommendation'                            AS recommendation{cat_col}
FROM latest l
JOIN calls c       ON c.id = l.call_id
LEFT JOIN managers m    ON m.id = c.manager_id
LEFT JOIN departments d ON d.id = m.department_id
CROSS JOIN LATERAL jsonb_array_elements(l.criteria->'per_criterion') AS cr{_FILTER};
"""


def upgrade() -> None:
    """Recreate both views with the client_category columns appended."""
    op.execute(_scores_view(category=True))
    op.execute(_criteria_view(category=True))


def downgrade() -> None:
    """Recreate both views without the client_category columns.

    ``CREATE OR REPLACE VIEW`` cannot *drop* columns, so the views are dropped
    first and recreated at their prior (category-free) shape.
    """
    op.execute("DROP VIEW IF EXISTS call_scores_latest")
    op.execute("DROP VIEW IF EXISTS call_criteria_latest")
    op.execute(_scores_view(category=False))
    op.execute(_criteria_view(category=False))
