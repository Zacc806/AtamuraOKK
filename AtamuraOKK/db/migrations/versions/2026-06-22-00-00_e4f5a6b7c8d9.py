"""Appeal flag dismissal: add dismissed_flags.

When a head accepts an appeal and awards a criterion full marks, they can also
clear the call's red flags that the appeal resolves (e.g. a presentation flag
once the presentation criterion is upheld). The cleared flag strings are stored
in ``dismissed_flags`` and hidden by the companion read layer.

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-06-22 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e4f5a6b7c8d9"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the dismissed_flags JSONB column to appeals."""
    op.add_column(
        "appeals",
        sa.Column("dismissed_flags", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    """Drop the dismissed_flags column."""
    op.drop_column("appeals", "dismissed_flags")
