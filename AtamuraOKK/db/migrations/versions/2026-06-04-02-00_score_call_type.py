"""Add call_type (контекстный режим) to scores.

Revision ID: d4e5f6a1b2c3
Revises: c3d4e5f6a1b2
Create Date: 2026-06-04 02:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "d4e5f6a1b2c3"
down_revision = "c3d4e5f6a1b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the scores.call_type column."""
    op.add_column(
        "scores",
        sa.Column(
            "call_type",
            sa.String(length=16),
            nullable=False,
            server_default="первичный",
        ),
    )
    op.create_index("ix_scores_call_type", "scores", ["call_type"])


def downgrade() -> None:
    """Drop the scores.call_type column."""
    op.drop_index("ix_scores_call_type", table_name="scores")
    op.drop_column("scores", "call_type")
