"""Add score_batches (in-flight Anthropic Message Batches of scoring requests).

Backs the batch scoring path (50% cheaper, results within 24h) used for the
non-urgent backlog. One row per submitted batch, holding the claimed call ids so
the poller can persist results and heartbeat the claims while the batch is open.

Revision ID: d5e3f2b1a8c9
Revises: c4d2e1a9f6b7
Create Date: 2026-08-04 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d5e3f2b1a8c9"
down_revision = "c4d2e1a9f6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the score_batches table."""
    op.create_table(
        "score_batches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider_batch_id", sa.String(length=128), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="in_flight",
        ),
        sa.Column("call_ids", postgresql.JSONB(), nullable=False),
        sa.Column("rubric_version", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("succeeded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errored", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_batch_id", name="uq_score_batches_provider_id"),
    )
    # The poller only ever scans open batches.
    op.create_index(
        "ix_score_batches_status",
        "score_batches",
        ["status"],
    )


def downgrade() -> None:
    """Drop the score_batches table."""
    op.drop_index("ix_score_batches_status", table_name="score_batches")
    op.drop_table("score_batches")
