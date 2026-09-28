"""recurring reminders and delivery preferences

Revision ID: 0019_recurring_reminders
Revises: 0018_object_storage
"""

import sqlalchemy as sa
from alembic import op

revision = "0019_recurring_reminders"
down_revision = "0018_object_storage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("reminders") as batch:
        batch.add_column(sa.Column("recurrence", sa.String(20), nullable=True))
        batch.add_column(sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"))
        batch.add_column(sa.Column("preferred_channel", sa.String(20), nullable=False, server_default="web"))
    with op.batch_alter_table("user_notifications") as batch:
        batch.add_column(sa.Column("delivery_channel", sa.String(20), nullable=False, server_default="both"))
        batch.create_index("ix_user_notifications_delivery_channel", ["delivery_channel"])


def downgrade() -> None:
    with op.batch_alter_table("user_notifications") as batch:
        batch.drop_index("ix_user_notifications_delivery_channel")
        batch.drop_column("delivery_channel")
    with op.batch_alter_table("reminders") as batch:
        batch.drop_column("preferred_channel")
        batch.drop_column("timezone")
        batch.drop_column("recurrence")
