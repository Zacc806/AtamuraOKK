"""Add scores.notified_at (cash-buyer manager-alert idempotency marker).

Set when a Bitrix personal notification is sent to the responsible manager for a
cash-paying client. Kept out of the score upsert payload so it survives a re-score
(a re-score must never re-notify). Nullable: NULL = not yet notified / not
applicable.

Revision ID: f8a9b0c1d2e3
Revises: f7a8b9c0d1e2
Create Date: 2026-06-16 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "f8a9b0c1d2e3"
down_revision = "f7a8b9c0d1e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the nullable notified_at column."""
    op.add_column(
        "scores",
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Drop the notified_at column."""
    op.drop_column("scores", "notified_at")
