"""Reporting views: call_scores_latest + call_criteria_latest.

Flatten the latest score per call (and its per-criterion breakdown) out of JSONB
into clean columns for Metabase, guaranteeing correct distributions even after
re-scoring (latest score wins via DISTINCT ON).

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-04 00:00:00.000000

"""

from alembic import op

revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


_CALL_SCORES_LATEST = """
CREATE VIEW call_scores_latest AS
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
    l.flags                                          AS red_flags
FROM latest l
JOIN calls c       ON c.id = l.call_id
LEFT JOIN managers m    ON m.id = c.manager_id
LEFT JOIN departments d ON d.id = m.department_id;
"""

_CALL_CRITERIA_LATEST = """
CREATE VIEW call_criteria_latest AS
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
    cr->>'evidence'                                  AS evidence
FROM latest l
JOIN calls c       ON c.id = l.call_id
LEFT JOIN managers m    ON m.id = c.manager_id
LEFT JOIN departments d ON d.id = m.department_id
CROSS JOIN LATERAL jsonb_array_elements(l.criteria->'per_criterion') AS cr;
"""


def upgrade() -> None:
    """Create the reporting views."""
    op.execute(_CALL_SCORES_LATEST)
    op.execute(_CALL_CRITERIA_LATEST)


def downgrade() -> None:
    """Drop the reporting views."""
    op.execute("DROP VIEW IF EXISTS call_criteria_latest;")
    op.execute("DROP VIEW IF EXISTS call_scores_latest;")
