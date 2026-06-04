"""Add call_type + is_qualification_call to call_scores_latest.

Lets reports exclude non-qualification calls (reminders, vendor/spam, internal,
wrong-number) from the team score so they don't distort it.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-04 01:00:00.000000

"""

from alembic import op

revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def _view(extra_cols: str) -> str:
    return f"""
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
    l.flags                                          AS red_flags{extra_cols}
FROM latest l
JOIN calls c       ON c.id = l.call_id
LEFT JOIN managers m    ON m.id = c.manager_id
LEFT JOIN departments d ON d.id = m.department_id;
"""


_EXTRA = """,
    (l.criteria->>'call_type')                       AS call_type,
    (l.criteria->>'is_qualification_call')::boolean  AS is_qualification_call"""


def upgrade() -> None:
    """Recreate call_scores_latest with the call-type columns."""
    op.execute("DROP VIEW IF EXISTS call_scores_latest;")
    op.execute(_view(_EXTRA))


def downgrade() -> None:
    """Recreate call_scores_latest without the call-type columns."""
    op.execute("DROP VIEW IF EXISTS call_scores_latest;")
    op.execute(_view(""))
