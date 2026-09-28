"""Explicit administrator role."""

import sqlalchemy as sa
from alembic import op

revision = "0009_admin_role"
down_revision = "0008_diagnosis_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("users", "is_admin")