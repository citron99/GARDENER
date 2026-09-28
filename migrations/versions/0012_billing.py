"""Stripe-backed subscriptions and idempotent billing events."""

import sqlalchemy as sa
from alembic import op

revision = "0012_billing"
down_revision = "0011_partner_members"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("plan", sa.String(30), nullable=False),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("provider_customer_id", sa.String(120), nullable=True, unique=True),
        sa.Column("provider_subscription_id", sa.String(120), nullable=True, unique=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_event_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_subscriptions_user_id", "subscriptions", ["user_id"], unique=True)
    op.create_index("ix_subscriptions_status", "subscriptions", ["status"])
    op.create_table(
        "billing_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider_event_id", sa.String(120), nullable=False, unique=True),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("processed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_billing_events_provider_event_id", "billing_events", ["provider_event_id"], unique=True)
    op.create_index("ix_billing_events_event_type", "billing_events", ["event_type"])


def downgrade() -> None:
    op.drop_table("billing_events")
    op.drop_table("subscriptions")