"""Add calls.source (telephony call vs ОП meeting) for scorer/rubric routing.

Revision ID: b7c8d9e0f1a2
Revises: e5f6a1b2c3d4
Create Date: 2026-06-04 12:00:00.000000

Additive + backward-compatible: a NOT NULL column with server_default
'bitrix_call', so every existing row and dashboard query is untouched. Meetings
are tagged 'op_meeting'.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b7c8d9e0f1a2"
down_revision = "e5f6a1b2c3d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the source column to calls."""
    op.add_column(
        "calls",
        sa.Column(
            "source",
            sa.String(length=16),
            nullable=False,
            server_default="bitrix_call",
        ),
    )
    op.create_index("ix_calls_source", "calls", ["source"])


def downgrade() -> None:
    """Drop the source column from calls."""
    op.drop_index("ix_calls_source", table_name="calls")
    op.drop_column("calls", "source")
