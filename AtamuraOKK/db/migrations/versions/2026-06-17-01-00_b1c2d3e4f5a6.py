"""Add appeals.disputed_block (the checklist block a manager contests).

The appeal form lets a manager mark one auto-review block they disagree with,
alongside their free-text feedback (``reason``). Nullable: NULL = the score is
disputed as a whole.

Revision ID: b1c2d3e4f5a6
Revises: a0b1c2d3e4f5
Create Date: 2026-06-17 01:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "b1c2d3e4f5a6"
down_revision = "a0b1c2d3e4f5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the nullable disputed_block column."""
    op.add_column(
        "appeals",
        sa.Column("disputed_block", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    """Drop the disputed_block column."""
    op.drop_column("appeals", "disputed_block")
