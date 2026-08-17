"""Add audit_batches (in-flight Message Batches of close-reason judgments).

Backs the batch judge path for the close-reason audit (50% cheaper, results usually
within an hour): the verdicts feed «Отказы не по делу», which nobody watches in
realtime, so paying list price per deal bought nothing. One row per submitted batch,
holding the Bitrix deal ids so the poller can finish their `audit_verdicts` rows —
which are written `verdict='pending'` at submit time and carry the deal context.

Revision ID: e6f4a3c2b9da
Revises: d5e3f2b1a8c9
Create Date: 2026-08-17 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e6f4a3c2b9da"
down_revision = "d5e3f2b1a8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the audit_batches table."""
    op.create_table(
        "audit_batches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider_batch_id", sa.String(length=128), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="in_flight",
        ),
        sa.Column("deal_ids", postgresql.JSONB(), nullable=False),
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
        sa.UniqueConstraint("provider_batch_id", name="uq_audit_batches_provider_id"),
    )
    # The poller only ever scans open batches.
    op.create_index(
        "ix_audit_batches_status",
        "audit_batches",
        ["status"],
    )


def downgrade() -> None:
    """Drop the audit_batches table."""
    op.drop_index("ix_audit_batches_status", table_name="audit_batches")
    op.drop_table("audit_batches")
