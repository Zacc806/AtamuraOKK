"""Add recommendation to call_criteria_latest.

The tm-call-v2 rubric scores 5 holistic criteria, each carrying a per-criterion
``recommendation`` (what the manager should improve next call) alongside the
existing justification/evidence. Expose it from the JSONB ``per_criterion`` so the
companion ``/api/v1`` read API can surface Claude's per-criterion feedback.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-06-09 01:00:00.000000

"""

from alembic import op

revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def _view(recommendation_col: str) -> str:
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
    cr->>'evidence'                                  AS evidence{recommendation_col}
FROM latest l
JOIN calls c       ON c.id = l.call_id
LEFT JOIN managers m    ON m.id = c.manager_id
LEFT JOIN departments d ON d.id = m.department_id
CROSS JOIN LATERAL jsonb_array_elements(l.criteria->'per_criterion') AS cr;
"""


_RECOMMENDATION = """,
    cr->>'recommendation'                            AS recommendation"""


def upgrade() -> None:
    """Recreate call_criteria_latest with the recommendation column."""
    op.execute(_view(_RECOMMENDATION))


def downgrade() -> None:
    """Recreate call_criteria_latest without the recommendation column."""
    op.execute(_view(""))
