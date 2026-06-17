"""Add appeals table (manager OKK-score re-check requests).

A manager files an appeal against a call's OKK score; the department head
reviews it and may record a corrected percent (``override_percent``) the
companion read layer prefers over the LLM percent. AtamuraOKK-owned table only;
no Bitrix or pipeline state involved.

Revision ID: a0b1c2d3e4f5
Revises: f8a9b0c1d2e3
Create Date: 2026-06-17 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "a0b1c2d3e4f5"
down_revision = "f8a9b0c1d2e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the appeals table + lookup indexes."""
    op.create_table(
        "appeals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("call_id", sa.Integer(), nullable=False),
        sa.Column("manager_bitrix_user_id", sa.BigInteger(), nullable=False),
        sa.Column("created_by_bitrix_user_id", sa.BigInteger(), nullable=False),
        sa.Column("department_bitrix_id", sa.BigInteger(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("override_percent", sa.Numeric(5, 2), nullable=True),
        sa.Column("head_note", sa.Text(), nullable=True),
        sa.Column("reviewed_by_bitrix_user_id", sa.BigInteger(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["call_id"], ["calls.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_appeals_call_id", "appeals", ["call_id"])
    op.create_index(
        "ix_appeals_manager_bitrix_user_id",
        "appeals",
        ["manager_bitrix_user_id"],
    )
    op.create_index(
        "ix_appeals_department_bitrix_id",
        "appeals",
        ["department_bitrix_id"],
    )
    op.create_index("ix_appeals_status", "appeals", ["status"])
    op.create_index("ix_appeals_created_at", "appeals", ["created_at"])


def downgrade() -> None:
    """Drop the appeals table."""
    op.drop_index("ix_appeals_created_at", "appeals")
    op.drop_index("ix_appeals_status", "appeals")
    op.drop_index("ix_appeals_department_bitrix_id", "appeals")
    op.drop_index("ix_appeals_manager_bitrix_user_id", "appeals")
    op.drop_index("ix_appeals_call_id", "appeals")
    op.drop_table("appeals")
