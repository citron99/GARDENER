"""Persistent idempotent user notifications."""

import sqlalchemy as sa
from alembic import op

revision = "0015_notifications"
down_revision = "0014_knowledge_vectors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deduplication_key", sa.String(240), nullable=False, unique=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("telegram_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivery_attempts", sa.Integer(), nullable=False),
        sa.Column("last_delivery_error", sa.String(300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("user_id", "kind", "event_at", "deduplication_key", "read_at", "telegram_sent_at"):
        op.create_index(f"ix_user_notifications_{column}", "user_notifications", [column], unique=column == "deduplication_key")


def downgrade() -> None:
    op.drop_table("user_notifications")