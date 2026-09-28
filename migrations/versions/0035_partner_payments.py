"""Add idempotent partner payment reconciliation records.

Revision ID: 0035_partner_payments
Revises: 0034_invoice_deliveries
"""

import sqlalchemy as sa
from alembic import op

revision = "0035_partner_payments"
down_revision = "0034_invoice_deliveries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "partner_payments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("invoice_id", sa.Integer(), nullable=True),
        sa.Column("imported_by_admin_id", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=60), nullable=False),
        sa.Column("external_id", sa.String(length=160), nullable=False),
        sa.Column("booking_date", sa.Date(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("reference", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["invoice_id"], ["partner_invoices.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["imported_by_admin_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "source", "external_id", name="uq_partner_payment_source_external"
        ),
    )
    op.create_index(
        "ix_partner_payments_invoice_id", "partner_payments", ["invoice_id"]
    )
    op.create_index(
        "ix_partner_payments_booking_date", "partner_payments", ["booking_date"]
    )
    op.create_index("ix_partner_payments_status", "partner_payments", ["status"])


def downgrade() -> None:
    op.drop_index("ix_partner_payments_status", table_name="partner_payments")
    op.drop_index("ix_partner_payments_booking_date", table_name="partner_payments")
    op.drop_index("ix_partner_payments_invoice_id", table_name="partner_payments")
    op.drop_table("partner_payments")
