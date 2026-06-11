"""Scored ОП meetings, mirrored from the meeting pipeline's SQLite state.

The meeting pipeline (``AtamuraOKK/scoring/meetings/``) keeps its working
state in SQLite; once a recording is SCORED its result is pushed here so the
companion cabinet (and Metabase) can read meetings next to calls. Attributed
to whoever uploaded the recording (``uploaded_by_bitrix_id`` → ``managers``);
``source`` distinguishes departments later.

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-06-11 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c9d0e1f2a3b4"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create meetings."""
    op.create_table(
        "meetings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("bitrix_file_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("folder_path", sa.Text(), nullable=True),
        sa.Column(
            "source",
            sa.String(length=32),
            server_default="op",
            nullable=False,
        ),
        sa.Column("uploaded_by_bitrix_id", sa.BigInteger(), nullable=True),
        sa.Column("manager_id", sa.Integer(), nullable=True),
        sa.Column("meeting_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_sec", sa.Integer(), nullable=True),
        sa.Column("language", sa.String(length=8), nullable=True),
        sa.Column("rubric_version", sa.String(length=64), nullable=True),
        sa.Column("score_pct", sa.Numeric(5, 2), nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=True),
        sa.Column("call_type", sa.String(length=32), nullable=True),
        sa.Column("manager_tone", sa.String(length=32), nullable=True),
        sa.Column(
            "needs_human_review",
            sa.Boolean(),
            server_default="false",
            nullable=False,
        ),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("red_flags", postgresql.JSONB(), nullable=True),
        sa.Column("score", postgresql.JSONB(), nullable=True),
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
        sa.ForeignKeyConstraint(["manager_id"], ["managers.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_meetings_bitrix_file_id",
        "meetings",
        ["bitrix_file_id"],
        unique=True,
    )
    op.create_index("ix_meetings_source", "meetings", ["source"])
    op.create_index(
        "ix_meetings_uploaded_by_bitrix_id",
        "meetings",
        ["uploaded_by_bitrix_id"],
    )
    op.create_index("ix_meetings_manager_id", "meetings", ["manager_id"])
    op.create_index("ix_meetings_meeting_at", "meetings", ["meeting_at"])


def downgrade() -> None:
    """Drop meetings."""
    op.drop_index("ix_meetings_meeting_at", table_name="meetings")
    op.drop_index("ix_meetings_manager_id", table_name="meetings")
    op.drop_index("ix_meetings_uploaded_by_bitrix_id", table_name="meetings")
    op.drop_index("ix_meetings_source", table_name="meetings")
    op.drop_index("ix_meetings_bitrix_file_id", table_name="meetings")
    op.drop_table("meetings")
