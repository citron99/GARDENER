"""Signed partner click attribution and idempotent leads.

Revision ID: 0026_partner_clicks
Revises: 0025_storage_journal
"""

import sqlalchemy as sa
from alembic import op

revision = "0026_partner_clicks"
down_revision = "0025_storage_journal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("partners") as batch:
        batch.add_column(sa.Column("postback_secret_hash", sa.String(64), nullable=True))
    with op.batch_alter_table("product_leads") as batch:
        batch.add_column(sa.Column("click_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("idempotency_key", sa.String(64), nullable=True))
        batch.add_column(sa.Column("redirected_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("redirect_count", sa.Integer(), nullable=False, server_default="0"))
        batch.create_index("ix_product_leads_click_id", ["click_id"], unique=True)
        batch.create_index("ix_product_leads_idempotency_key", ["idempotency_key"], unique=True)
        batch.create_index("ix_product_leads_redirected_at", ["redirected_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("product_leads") as batch:
        batch.drop_index("ix_product_leads_redirected_at")
        batch.drop_index("ix_product_leads_idempotency_key")
        batch.drop_index("ix_product_leads_click_id")
        batch.drop_column("redirect_count")
        batch.drop_column("redirected_at")
        batch.drop_column("idempotency_key")
        batch.drop_column("click_id")
    with op.batch_alter_table("partners") as batch:
        batch.drop_column("postback_secret_hash")
