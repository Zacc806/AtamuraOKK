"""Widen meetings.call_type 32 -> 64.

The meeting scorer is meant to return a short ``call_type`` enum
(первичный | повторный | уточняющий | сервисный), but a chatty LLM
occasionally returns a descriptive phrase that overflows VARCHAR(32) and
crashes the push upsert, leaving SCORED meetings stuck unpushed. Widen the
column for headroom; the push stage now also clamps defensively.

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-06-18 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "c2d3e4f5a6b7"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Widen call_type to 64 chars."""
    op.alter_column(
        "meetings",
        "call_type",
        existing_type=sa.String(length=32),
        type_=sa.String(length=64),
        existing_nullable=True,
    )


def downgrade() -> None:
    """Narrow call_type back to 32 chars (truncates over-long values)."""
    op.alter_column(
        "meetings",
        "call_type",
        existing_type=sa.String(length=64),
        type_=sa.String(length=32),
        existing_nullable=True,
    )
