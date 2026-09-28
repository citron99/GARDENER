"""Telegram account linking and idempotent webhook updates."""

import sqlalchemy as sa
from alembic import op

revision = "0013_telegram"
down_revision = "0012_billing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "telegram_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("username", sa.String(64), nullable=True),
        sa.Column("language", sa.String(10), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_telegram_accounts_user_id", "telegram_accounts", ["user_id"], unique=True)
    op.create_index("ix_telegram_accounts_chat_id", "telegram_accounts", ["chat_id"], unique=True)
    op.create_index("ix_telegram_accounts_active", "telegram_accounts", ["active"])
    op.create_table(
        "telegram_link_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_telegram_link_tokens_user_id", "telegram_link_tokens", ["user_id"])
    op.create_index("ix_telegram_link_tokens_token_hash", "telegram_link_tokens", ["token_hash"], unique=True)
    op.create_index("ix_telegram_link_tokens_expires_at", "telegram_link_tokens", ["expires_at"])
    op.create_table(
        "telegram_updates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("update_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("response_text", sa.Text(), nullable=True),
        sa.Column("sent", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_telegram_updates_update_id", "telegram_updates", ["update_id"], unique=True)
    op.create_index("ix_telegram_updates_chat_id", "telegram_updates", ["chat_id"])
    op.create_index("ix_telegram_updates_sent", "telegram_updates", ["sent"])


def downgrade() -> None:
    op.drop_table("telegram_updates")
    op.drop_table("telegram_link_tokens")
    op.drop_table("telegram_accounts")