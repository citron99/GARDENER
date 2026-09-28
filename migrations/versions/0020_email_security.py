"""email verification and password recovery

Revision ID: 0020_email_security
Revises: 0019_recurring_reminders
"""

import sqlalchemy as sa
from alembic import op

revision = "0020_email_security"
down_revision = "0019_recurring_reminders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_index("ix_users_email_verified_at", ["email_verified_at"])
    op.create_table(
        "account_action_tokens",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("purpose", sa.String(30), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("user_id", "purpose", "expires_at"):
        op.create_index(f"ix_account_action_tokens_{column}", "account_action_tokens", [column])
    op.create_index("ix_account_action_tokens_token_hash", "account_action_tokens", ["token_hash"], unique=True)


def downgrade() -> None:
    op.drop_table("account_action_tokens")
    with op.batch_alter_table("users") as batch:
        batch.drop_index("ix_users_email_verified_at")
        batch.drop_column("email_verified_at")
