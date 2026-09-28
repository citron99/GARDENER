"""Plant care journal events."""

import sqlalchemy as sa
from alembic import op

revision = "0006_care_events"
down_revision = "0005_ai_request_logs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "care_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("plant_id", sa.Integer(), sa.ForeignKey("plants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(30), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("amount", sa.Float(), nullable=True),
        sa.Column("unit", sa.String(30), nullable=True),
        sa.Column("product", sa.String(160), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_care_events_plant_id", "care_events", ["plant_id"])
    op.create_index("ix_care_events_event_type", "care_events", ["event_type"])
    op.create_index("ix_care_events_occurred_at", "care_events", ["occurred_at"])


def downgrade() -> None:
    op.drop_index("ix_care_events_occurred_at", table_name="care_events")
    op.drop_index("ix_care_events_event_type", table_name="care_events")
    op.drop_index("ix_care_events_plant_id", table_name="care_events")
    op.drop_table("care_events")