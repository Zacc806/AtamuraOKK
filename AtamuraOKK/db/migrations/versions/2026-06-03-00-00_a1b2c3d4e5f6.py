"""Phase 1 schema: departments, managers, calls, transcripts, scores, rubrics.

Drops the template's dummy_model and creates the call-analysis schema.

Revision ID: a1b2c3d4e5f6
Revises: 2b7380507a71
Create Date: 2026-06-03 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "a1b2c3d4e5f6"
down_revision = "2b7380507a71"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Run the upgrade migrations."""
    op.drop_table("dummy_model")

    op.create_table(
        "departments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("bitrix_id", sa.BigInteger(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("head_user_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_departments_bitrix_id", "departments", ["bitrix_id"], unique=True,
    )

    op.create_table(
        "managers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("bitrix_user_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("last_name", sa.String(length=255), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("department_id", sa.Integer(), nullable=True),
        sa.Column(
            "active", sa.Boolean(), server_default=sa.text("true"), nullable=False,
        ),
        sa.Column(
            "enriched", sa.Boolean(), server_default=sa.text("false"), nullable=False,
        ),
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
        sa.ForeignKeyConstraint(
            ["department_id"], ["departments.id"], ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_managers_bitrix_user_id", "managers", ["bitrix_user_id"], unique=True,
    )
    op.create_index(
        "ix_managers_department_id", "managers", ["department_id"], unique=False,
    )

    op.create_table(
        "calls",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("bitrix_call_id", sa.String(length=255), nullable=False),
        sa.Column("bitrix_row_id", sa.BigInteger(), nullable=True),
        sa.Column("portal_user_id", sa.BigInteger(), nullable=True),
        sa.Column("manager_id", sa.Integer(), nullable=True),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_sec", sa.Integer(), nullable=False),
        sa.Column("phone_number", sa.String(length=64), nullable=True),
        sa.Column("crm_entity_type", sa.String(length=32), nullable=True),
        sa.Column("crm_entity_id", sa.BigInteger(), nullable=True),
        sa.Column("crm_activity_id", sa.BigInteger(), nullable=True),
        sa.Column("client_key", sa.String(length=128), nullable=True),
        sa.Column("recording_url", sa.Text(), nullable=True),
        sa.Column("record_file_id", sa.BigInteger(), nullable=True),
        sa.Column("audio_object_key", sa.String(length=512), nullable=True),
        sa.Column("is_stereo", sa.Boolean(), nullable=True),
        sa.Column("language", sa.String(length=8), nullable=True),
        sa.Column(
            "is_first_call",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("client_qualified", sa.Boolean(), nullable=True),
        sa.Column(
            "analyzable",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'NEW'"),
            nullable=False,
        ),
        sa.Column("skip_reason", sa.String(length=64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "attempts", sa.Integer(), server_default=sa.text("0"), nullable=False,
        ),
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
        sa.ForeignKeyConstraint(
            ["manager_id"], ["managers.id"], ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_calls_bitrix_call_id", "calls", ["bitrix_call_id"], unique=True,
    )
    for col in (
        "bitrix_row_id",
        "portal_user_id",
        "manager_id",
        "started_at",
        "phone_number",
        "client_key",
        "analyzable",
        "status",
    ):
        op.create_index(f"ix_calls_{col}", "calls", [col], unique=False)

    op.create_table(
        "transcripts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("call_id", sa.Integer(), nullable=False),
        sa.Column("language", sa.String(length=8), nullable=True),
        sa.Column("full_text", sa.Text(), nullable=False),
        sa.Column("segments", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["call_id"], ["calls.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_transcripts_call_id", "transcripts", ["call_id"], unique=True)

    op.create_table(
        "scores",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("call_id", sa.Integer(), nullable=False),
        sa.Column("rubric_version", sa.String(length=64), nullable=True),
        sa.Column("total_score", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("criteria", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("sentiment", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("flags", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["call_id"], ["calls.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scores_call_id", "scores", ["call_id"], unique=False)
    op.create_index("ix_scores_created_at", "scores", ["created_at"], unique=False)

    op.create_table(
        "rubric_versions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column(
            "definition", postgresql.JSONB(astext_type=sa.Text()), nullable=False,
        ),
        sa.Column(
            "active", sa.Boolean(), server_default=sa.text("false"), nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_rubric_versions_version", "rubric_versions", ["version"], unique=True,
    )

    op.create_table(
        "ingest_state",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("last_cursor", sa.String(length=64), nullable=True),
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
    """Run the downgrade migrations."""
    op.drop_table("ingest_state")
    op.drop_table("rubric_versions")
    op.drop_table("scores")
    op.drop_table("transcripts")
    op.drop_table("calls")
    op.drop_table("managers")
    op.drop_table("departments")

    op.create_table(
        "dummy_model",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
