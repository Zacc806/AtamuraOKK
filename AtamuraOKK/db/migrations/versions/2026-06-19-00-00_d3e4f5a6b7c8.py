"""Per-criterion appeals: add disputed_criteria / confirmed_criteria.

An appeal now lists the specific rubric criteria a manager contests
(``disputed_criteria``); the head confirms a subset (``confirmed_criteria``),
each awarded full marks, and ``override_percent`` is recomputed from that. The
legacy single ``disputed_block`` column stays nullable for old rows.

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-06-19 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d3e4f5a6b7c8"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the two per-criterion JSONB columns to appeals."""
    op.add_column(
        "appeals",
        sa.Column("disputed_criteria", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "appeals",
        sa.Column("confirmed_criteria", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    """Drop the per-criterion columns."""
    op.drop_column("appeals", "confirmed_criteria")
    op.drop_column("appeals", "disputed_criteria")
