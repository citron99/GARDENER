"""Add audited manual partner payment resolution.

Revision ID: 0036_payment_resolution
Revises: 0035_partner_payments
"""

import sqlalchemy as sa
from alembic import op

revision = "0036_payment_resolution"
down_revision = "0035_partner_payments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("partner_payments") as batch:
        batch.add_column(
            sa.Column("resolution_note", sa.String(length=500), nullable=True)
        )
        batch.add_column(sa.Column("resolved_by_admin_id", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.create_foreign_key(
            "fk_partner_payment_resolved_by_admin",
            "users",
            ["resolved_by_admin_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("partner_payments") as batch:
        batch.drop_constraint(
            "fk_partner_payment_resolved_by_admin", type_="foreignkey"
        )
        batch.drop_column("resolved_at")
        batch.drop_column("resolved_by_admin_id")
        batch.drop_column("resolution_note")
