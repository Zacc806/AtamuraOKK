"""Add source (telephony / whatsapp) to calls.

Revision ID: c3d4e5f6a1b2
Revises: b2c3d4e5f6a1
Create Date: 2026-06-04 01:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c3d4e5f6a1b2"
down_revision = "b2c3d4e5f6a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the calls.source classification column."""
    op.add_column(
        "calls",
        sa.Column(
            "source",
            sa.String(length=16),
            nullable=False,
            server_default="telephony",
        ),
    )
    op.create_index("ix_calls_source", "calls", ["source"])


def downgrade() -> None:
    """Drop the calls.source column."""
    op.drop_index("ix_calls_source", table_name="calls")
    op.drop_column("calls", "source")
