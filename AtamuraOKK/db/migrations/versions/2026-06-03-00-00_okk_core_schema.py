"""OKK core schema: departments, managers, calls, transcripts, scores, rubric_versions, ingest_state.

Revision ID: f1a2b3c4d5e6
Revises: 2b7380507a71
Create Date: 2026-06-03 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "f1a2b3c4d5e6"
down_revision = "2b7380507a71"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the OKK core tables."""
    op.create_table(
        "departments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("bitrix_dept_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("head_bitrix_user_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_departments_bitrix_dept_id",
        "departments",
        ["bitrix_dept_id"],
        unique=True,
    )

    op.create_table(
        "managers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("bitrix_user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("department_id", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
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
        sa.ForeignKeyConstraint(["department_id"], ["departments.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_managers_bitrix_user_id",
        "managers",
        ["bitrix_user_id"],
        unique=True,
    )
    op.create_index("ix_managers_department_id", "managers", ["department_id"])

    op.create_table(
        "calls",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("bitrix_call_id", sa.String(length=64), nullable=False),
        sa.Column("manager_id", sa.Integer(), nullable=True),
        sa.Column("direction", sa.SmallInteger(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_sec", sa.Integer(), nullable=False),
        sa.Column("failed_code", sa.String(length=16), nullable=True),
        sa.Column("record_file_id", sa.String(length=64), nullable=True),
        sa.Column("record_url", sa.String(length=2000), nullable=True),
        sa.Column("audio_path", sa.String(length=1024), nullable=True),
        sa.Column("is_stereo", sa.Boolean(), nullable=True),
        sa.Column("crm_entity_type", sa.String(length=32), nullable=True),
        sa.Column("crm_entity_id", sa.Integer(), nullable=True),
        sa.Column("crm_activity_id", sa.Integer(), nullable=True),
        sa.Column("phone_number", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.String(length=1024), nullable=True),
        sa.Column("failed_stage", sa.String(length=32), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
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
        sa.ForeignKeyConstraint(["manager_id"], ["managers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_calls_bitrix_call_id", "calls", ["bitrix_call_id"], unique=True)
    op.create_index("ix_calls_status", "calls", ["status"])
    op.create_index("ix_calls_started_at", "calls", ["started_at"])
    op.create_index("ix_calls_status_id", "calls", ["status", "id"])
    op.create_index("ix_calls_manager_started", "calls", ["manager_id", "started_at"])
    op.create_index(
        "ix_calls_crm_entity",
        "calls",
        ["crm_entity_type", "crm_entity_id"],
    )

    op.create_table(
        "transcripts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("call_id", sa.Integer(), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=False),
        sa.Column("language_probability", sa.Float(), nullable=True),
        sa.Column("full_text", sa.Text(), nullable=False),
        sa.Column("segments", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["call_id"], ["calls.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_transcripts_call_id", "transcripts", ["call_id"], unique=True)

    op.create_table(
        "scores",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("call_id", sa.Integer(), nullable=False),
        sa.Column("transcript_id", sa.Integer(), nullable=True),
        sa.Column("rubric_version", sa.String(length=64), nullable=False),
        sa.Column("total_score", sa.Integer(), nullable=False),
        sa.Column("max_total", sa.Integer(), nullable=False),
        sa.Column("score_pct", sa.Float(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("criteria", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("client_agreed_meeting", sa.Boolean(), nullable=False),
        sa.Column("manager_tone", sa.String(length=32), nullable=False),
        sa.Column("red_flags", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=8), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("needs_human_review", sa.Boolean(), nullable=False),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["call_id"], ["calls.id"]),
        sa.ForeignKeyConstraint(["transcript_id"], ["transcripts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scores_call_id", "scores", ["call_id"])
    op.create_index("ix_scores_rubric_version", "scores", ["rubric_version"])
    op.create_index("ix_scores_score_pct", "scores", ["score_pct"])

    op.create_table(
        "rubric_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column(
            "definition",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_rubric_versions_version",
        "rubric_versions",
        ["version"],
        unique=True,
    )

    op.create_table(
        "ingest_state",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("last_call_id", sa.BigInteger(), nullable=False),
        sa.Column("last_window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ingest_state_key", "ingest_state", ["key"], unique=True)


def downgrade() -> None:
    """Drop the OKK core tables."""
    op.drop_table("ingest_state")
    op.drop_table("rubric_versions")
    op.drop_table("scores")
    op.drop_table("transcripts")
    op.drop_table("calls")
    op.drop_table("managers")
    op.drop_table("departments")
