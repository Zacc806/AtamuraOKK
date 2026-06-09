"""Add calls.claimed_at + partial claim index for distributed workers.

Supports the broker-based distributed worker rollout: workers atomically claim
rows into an in-flight status (DOWNLOADING/TRANSCRIBING/SCORING) using
``SELECT ... FOR UPDATE SKIP LOCKED``. ``claimed_at`` lets a stale-claim
reconciler revert claims left behind by a crashed worker; the partial index
keeps the dispatcher's "ready rows" scan cheap as the table grows.

The in-flight statuses themselves need no schema change (``calls.status`` is a
``VARCHAR(16)``, not a Postgres enum).

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-06-08 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None

_CLAIM_INDEX = "ix_calls_claimable"


def upgrade() -> None:
    """Add claimed_at and the partial (status, started_at) claim index."""
    op.add_column(
        "calls",
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        _CLAIM_INDEX,
        "calls",
        ["status", "started_at"],
        postgresql_where=sa.text("analyzable = true"),
    )


def downgrade() -> None:
    """Drop the claim index and claimed_at column."""
    op.drop_index(_CLAIM_INDEX, table_name="calls")
    op.drop_column("calls", "claimed_at")
