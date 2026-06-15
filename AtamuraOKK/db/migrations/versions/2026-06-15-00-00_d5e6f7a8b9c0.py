"""Add calls.client_category (manager-assigned A/B/C/X lead category).

The category is sourced from a Bitrix Contact/Lead custom field at ingestion time
and tunes the meeting-closing criterion at scoring time (B reduced, C excluded).
Nullable: NULL when untagged or unresolvable (phone-only clients), treated as full
weight.

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-06-15 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "d5e6f7a8b9c0"
down_revision = "c4d5e6f7a8b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the nullable client_category column."""
    op.add_column(
        "calls",
        sa.Column("client_category", sa.String(length=8), nullable=True),
    )


def downgrade() -> None:
    """Drop the client_category column."""
    op.drop_column("calls", "client_category")
