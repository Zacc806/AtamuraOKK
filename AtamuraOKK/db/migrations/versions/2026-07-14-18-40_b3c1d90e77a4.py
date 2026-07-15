"""add audit_verdicts.details (deterministic-check evidence).

Revision ID: b3c1d90e77a4
Revises: 24254a1f2f2c
Create Date: 2026-07-14 18:40:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "b3c1d90e77a4"
down_revision = "24254a1f2f2c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the details column (duplicate-check evidence; NULL for LLM verdicts)."""
    op.add_column(
        "audit_verdicts",
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    """Drop the details column."""
    op.drop_column("audit_verdicts", "details")
