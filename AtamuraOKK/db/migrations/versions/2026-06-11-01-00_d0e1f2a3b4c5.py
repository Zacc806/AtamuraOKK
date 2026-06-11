"""Per-source rubrics: one active rubric per department axis.

Calls (source "tm") and ОП meetings (source "op") are scored against
different criteria, so ``rubric_versions`` gains a ``source`` column and the
"one active rubric globally" rule becomes "one active rubric per source".
Existing rows are the call rubric — the ``server_default="tm"`` backfills
them. ``version`` stops being globally unique; ``(source, version)`` is the
new identity (and the seed's upsert conflict target).

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-06-11 01:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "d0e1f2a3b4c5"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add rubric_versions.source; scope uniqueness + activeness per source."""
    op.add_column(
        "rubric_versions",
        sa.Column("source", sa.String(length=32), server_default="tm", nullable=False),
    )
    op.create_index("ix_rubric_versions_source", "rubric_versions", ["source"])
    op.drop_index("ix_rubric_versions_version", table_name="rubric_versions")
    op.create_index("ix_rubric_versions_version", "rubric_versions", ["version"])
    op.create_unique_constraint(
        "uq_rubric_versions_source_version",
        "rubric_versions",
        ["source", "version"],
    )
    # At most one row is active today, so the partial index is satisfied.
    op.create_index(
        "uq_rubric_versions_active_per_source",
        "rubric_versions",
        ["source"],
        unique=True,
        postgresql_where=sa.text("active"),
    )


def downgrade() -> None:
    """Drop the source axis; restore the globally-unique version."""
    op.drop_index("uq_rubric_versions_active_per_source", table_name="rubric_versions")
    op.drop_constraint(
        "uq_rubric_versions_source_version",
        "rubric_versions",
        type_="unique",
    )
    op.drop_index("ix_rubric_versions_version", table_name="rubric_versions")
    op.create_index(
        "ix_rubric_versions_version",
        "rubric_versions",
        ["version"],
        unique=True,
    )
    op.drop_index("ix_rubric_versions_source", table_name="rubric_versions")
    op.drop_column("rubric_versions", "source")
