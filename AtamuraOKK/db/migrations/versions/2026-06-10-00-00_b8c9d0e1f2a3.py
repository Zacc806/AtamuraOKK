"""Companion cabinet users: personal access keys + roles.

The companion read API moves from a single shared bearer (anyone in the cabinet
could request any manager's data) to per-user identity: a ``companion_users``
row binds a SHA-256-hashed personal key to a role — ``manager`` (sees only own
data) or ``head`` (руководитель отдела продаж, sees everything). Keys are
issued via ``python -m AtamuraOKK.companion_users``.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-06-10 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "b8c9d0e1f2a3"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create companion_users."""
    op.create_table(
        "companion_users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("key_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "role",
            sa.String(length=16),
            server_default="manager",
            nullable=False,
        ),
        sa.Column("bitrix_user_id", sa.BigInteger(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_companion_users_key_sha256",
        "companion_users",
        ["key_sha256"],
        unique=True,
    )
    op.create_index(
        "ix_companion_users_bitrix_user_id",
        "companion_users",
        ["bitrix_user_id"],
    )


def downgrade() -> None:
    """Drop companion_users."""
    op.drop_index("ix_companion_users_bitrix_user_id", table_name="companion_users")
    op.drop_index("ix_companion_users_key_sha256", table_name="companion_users")
    op.drop_table("companion_users")
