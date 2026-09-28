"""Stage account deletion and retry storage cleanup.

Revision ID: 0024_account_delete
Revises: 0023_ai_safety
"""

import sqlalchemy as sa
from alembic import op

revision = "0024_account_delete"
down_revision = "0023_ai_safety"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "account_deletion_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("provider_subscription_id", sa.String(120), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(300), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancellation_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", name="uq_account_deletion_request_user"),
    )
    op.create_index("ix_account_deletion_requests_user_id", "account_deletion_requests", ["user_id"])
    op.create_index("ix_account_deletion_requests_status", "account_deletion_requests", ["status"])
    op.create_table(
        "storage_deletion_outbox",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("backend", sa.String(20), nullable=False),
        sa.Column("storage_key", sa.String(500), nullable=True),
        sa.Column("file_path", sa.String(500), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(300), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_storage_deletion_outbox_status", "storage_deletion_outbox", ["status"])
    op.create_index("ix_storage_deletion_outbox_next_attempt_at", "storage_deletion_outbox", ["next_attempt_at"])


def downgrade() -> None:
    op.drop_table("storage_deletion_outbox")
    op.drop_table("account_deletion_requests")
