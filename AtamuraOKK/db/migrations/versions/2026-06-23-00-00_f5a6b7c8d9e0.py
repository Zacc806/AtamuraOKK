"""Transcript role reconciliation: add manager_side_applied.

The stereo channel->role guess is sometimes inverted, so the manager ends up
labeled as the customer (and vice versa) on the stored transcript. The scorer
now reports which side it identified as the manager (CallScore.manager_side),
and the scoring worker reconciles the stored speaker labels accordingly. This
boolean records that the reconciliation has been applied so a re-score never
double-flips the labels.

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
Create Date: 2026-06-23 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "f5a6b7c8d9e0"
down_revision = "e4f5a6b7c8d9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the manager_side_applied flag to transcripts (default false)."""
    op.add_column(
        "transcripts",
        sa.Column(
            "manager_side_applied",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    """Drop the manager_side_applied column."""
    op.drop_column("transcripts", "manager_side_applied")
