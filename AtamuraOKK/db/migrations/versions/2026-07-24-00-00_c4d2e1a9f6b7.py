"""Add scores.manager_spoken_name (name the manager voiced on the call).

Extracted by the scorer for reconciliation against the CRM-attributed manager
(calls.manager_id stays authoritative). Nullable: NULL = the manager gave no name
on the call. Refreshed on re-score like every other content-derived score field.

Revision ID: c4d2e1a9f6b7
Revises: b3c1d90e77a4
Create Date: 2026-07-24 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "c4d2e1a9f6b7"
down_revision = "b3c1d90e77a4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the nullable manager_spoken_name column."""
    op.add_column(
        "scores",
        sa.Column("manager_spoken_name", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    """Drop the manager_spoken_name column."""
    op.drop_column("scores", "manager_spoken_name")
