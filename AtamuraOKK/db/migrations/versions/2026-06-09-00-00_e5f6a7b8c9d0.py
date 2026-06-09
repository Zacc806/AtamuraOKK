"""Deduplicate scores and enforce one row per (call_id, rubric_version).

A re-claim (stale-claim reconciler reverting a still-running scoring job) or a
duplicate broker delivery could insert a second ``scores`` row for the same call.
The reporting views dedupe with ``DISTINCT ON (call_id)``, so this stayed silent
while double-spending the LLM budget and bloating the table. This migration
removes the accumulated duplicates (keeping the latest per call+rubric) and adds
a unique constraint so the scoring worker can upsert instead of insert.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-09 00:00:00.000000

"""

from alembic import op

revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None

_CONSTRAINT = "uq_scores_call_rubric"


def upgrade() -> None:
    """Drop duplicate score rows, then add the uniqueness constraint."""
    # Keep the newest row per (call_id, rubric_version); NULL rubric versions are
    # grouped together via IS NOT DISTINCT FROM.
    op.execute(
        """
        DELETE FROM scores s
        USING scores s2
        WHERE s.call_id = s2.call_id
          AND s.rubric_version IS NOT DISTINCT FROM s2.rubric_version
          AND (s.created_at, s.id) < (s2.created_at, s2.id)
        """,
    )
    op.create_unique_constraint(
        _CONSTRAINT,
        "scores",
        ["call_id", "rubric_version"],
    )


def downgrade() -> None:
    """Drop the uniqueness constraint (duplicates are not restored)."""
    op.drop_constraint(_CONSTRAINT, "scores", type_="unique")
