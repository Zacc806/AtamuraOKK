"""Add scoring-subsystem fields to scores (score_pct, call_type, script_*, ...).

Revision ID: e5f6a1b2c3d4
Revises: a1b2c3d4e5f6
Create Date: 2026-06-04 10:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "e5f6a1b2c3d4"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None

_JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    """Add the scoring-subsystem columns to scores."""
    op.add_column("scores", sa.Column("score_pct", sa.Float(), nullable=True))
    op.add_column("scores", sa.Column("max_total", sa.Integer(), nullable=True))
    op.add_column("scores", sa.Column("passed", sa.Boolean(), nullable=True))
    op.add_column("scores", sa.Column("call_type", sa.String(length=16), nullable=True))
    op.add_column(
        "scores",
        sa.Column("client_agreed_meeting", sa.Boolean(), nullable=True),
    )
    op.add_column("scores", sa.Column("manager_tone", sa.String(length=32), nullable=True))
    op.add_column("scores", sa.Column("language", sa.String(length=8), nullable=True))
    op.add_column("scores", sa.Column("provider", sa.String(length=16), nullable=True))
    op.add_column(
        "scores",
        sa.Column(
            "needs_human_review",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.add_column("scores", sa.Column("script_adherence", sa.Float(), nullable=True))
    op.add_column("scores", sa.Column("script_deviations", _JSONB, nullable=True))
    op.add_column("scores", sa.Column("meta", _JSONB, nullable=True))
    op.create_index("ix_scores_score_pct", "scores", ["score_pct"])
    op.create_index("ix_scores_call_type", "scores", ["call_type"])
    op.create_index("ix_scores_rubric_version", "scores", ["rubric_version"])


def downgrade() -> None:
    """Drop the scoring-subsystem columns."""
    op.drop_index("ix_scores_rubric_version", table_name="scores")
    op.drop_index("ix_scores_call_type", table_name="scores")
    op.drop_index("ix_scores_score_pct", table_name="scores")
    for col in (
        "meta",
        "script_deviations",
        "script_adherence",
        "needs_human_review",
        "provider",
        "language",
        "manager_tone",
        "client_agreed_meeting",
        "call_type",
        "passed",
        "max_total",
        "score_pct",
    ):
        op.drop_column("scores", col)
