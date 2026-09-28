"""Plant reminders and calendar tasks."""

import sqlalchemy as sa
from alembic import op

revision = "0007_reminders"
down_revision = "0006_care_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reminders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("plant_id", sa.Integer(), sa.ForeignKey("plants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_reminders_plant_id", "reminders", ["plant_id"])
    op.create_index("ix_reminders_kind", "reminders", ["kind"])
    op.create_index("ix_reminders_due_at", "reminders", ["due_at"])
    op.create_index("ix_reminders_completed_at", "reminders", ["completed_at"])


def downgrade() -> None:
    op.drop_index("ix_reminders_completed_at", table_name="reminders")
    op.drop_index("ix_reminders_due_at", table_name="reminders")
    op.drop_index("ix_reminders_kind", table_name="reminders")
    op.drop_index("ix_reminders_plant_id", table_name="reminders")
    op.drop_table("reminders")