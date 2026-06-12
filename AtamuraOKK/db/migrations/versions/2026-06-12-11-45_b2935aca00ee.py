"""calls: add client_qualified_at (until-qualified scope).

Revision ID: b2935aca00ee
Revises: d0e1f2a3b4c5
Create Date: 2026-06-12 11:45:57.170124

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b2935aca00ee"
down_revision = "d0e1f2a3b4c5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Run the migration."""
    # NB: autogenerate also suggested dropping ix_calls_claimable — that partial
    # index is hand-made (d4e5f6a7b8c9), lives outside the models on purpose.
    op.add_column(
        "calls",
        sa.Column("client_qualified_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Undo the migration."""
    op.drop_column("calls", "client_qualified_at")
