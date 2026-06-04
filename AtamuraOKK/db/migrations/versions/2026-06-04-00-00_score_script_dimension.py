"""Add script-adherence dimension to scores.

Revision ID: b2c3d4e5f6a1
Revises: f1a2b3c4d5e6
Create Date: 2026-06-04 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "b2c3d4e5f6a1"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add script_adherence + script_deviations to scores."""
    op.add_column("scores", sa.Column("script_adherence", sa.Float(), nullable=True))
    op.add_column(
        "scores",
        sa.Column(
            "script_deviations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    """Drop the script dimension columns."""
    op.drop_column("scores", "script_deviations")
    op.drop_column("scores", "script_adherence")
