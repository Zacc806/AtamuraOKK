"""companion_users: add department_id (department-scoped head keys).

Revision ID: c4d5e6f7a8b9
Revises: b2935aca00ee
Create Date: 2026-06-12 13:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c4d5e6f7a8b9"
down_revision = "b2935aca00ee"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Run the migration."""
    op.add_column(
        "companion_users",
        sa.Column("department_id", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    """Undo the migration."""
    op.drop_column("companion_users", "department_id")
