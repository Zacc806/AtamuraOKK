"""add audit_verdicts.

Revision ID: 24254a1f2f2c
Revises: f5a6b7c8d9e0
Create Date: 2026-07-03 15:49:37.292440

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "24254a1f2f2c"
down_revision = "f5a6b7c8d9e0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create audit_verdicts."""
    op.create_table(
        "audit_verdicts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("bitrix_deal_id", sa.BigInteger(), nullable=False),
        sa.Column("deal_title", sa.String(length=512), nullable=True),
        sa.Column("manager_id", sa.Integer(), nullable=True),
        sa.Column("assigned_by_id", sa.BigInteger(), nullable=True),
        sa.Column("client_key", sa.String(length=128), nullable=True),
        sa.Column("close_reason", sa.String(length=255), nullable=True),
        sa.Column("reason_id", sa.String(length=64), nullable=True),
        sa.Column("verdict", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("justification", sa.Text(), nullable=True),
        sa.Column("evidence_quote", sa.Text(), nullable=True),
        sa.Column("call_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "audited_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["manager_id"], ["managers.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bitrix_deal_id", name="uq_audit_verdicts_deal"),
    )
    op.create_index(
        op.f("ix_audit_verdicts_audited_at"),
        "audit_verdicts",
        ["audited_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_audit_verdicts_bitrix_deal_id"),
        "audit_verdicts",
        ["bitrix_deal_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_audit_verdicts_manager_id"),
        "audit_verdicts",
        ["manager_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_audit_verdicts_verdict"), "audit_verdicts", ["verdict"], unique=False
    )


def downgrade() -> None:
    """Drop audit_verdicts."""
    op.drop_index(op.f("ix_audit_verdicts_verdict"), table_name="audit_verdicts")
    op.drop_index(op.f("ix_audit_verdicts_manager_id"), table_name="audit_verdicts")
    op.drop_index(op.f("ix_audit_verdicts_bitrix_deal_id"), table_name="audit_verdicts")
    op.drop_index(op.f("ix_audit_verdicts_audited_at"), table_name="audit_verdicts")
    op.drop_table("audit_verdicts")
