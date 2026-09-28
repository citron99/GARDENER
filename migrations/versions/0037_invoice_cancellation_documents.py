"""Add immutable invoice cancellation documents.

Revision ID: 0037_invoice_cancellations
Revises: 0036_payment_resolution
"""

import sqlalchemy as sa
from alembic import op

revision = "0037_invoice_cancellations"
down_revision = "0036_payment_resolution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("partner_invoices") as batch:
        batch.add_column(
            sa.Column("cancellation_number", sa.String(length=50), nullable=True)
        )
        batch.add_column(
            sa.Column("cancellation_reason", sa.String(length=500), nullable=True)
        )
        batch.add_column(
            sa.Column(
                "cancellation_document_version", sa.String(length=40), nullable=True
            )
        )
        batch.add_column(
            sa.Column("cancellation_pdf_sha256", sa.String(length=64), nullable=True)
        )
        batch.add_column(
            sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(
            sa.Column("cancelled_by_admin_id", sa.Integer(), nullable=True)
        )
        batch.create_foreign_key(
            "fk_partner_invoice_cancelled_by_admin",
            "users",
            ["cancelled_by_admin_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index(
            "ix_partner_invoices_cancellation_number",
            ["cancellation_number"],
            unique=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("partner_invoices") as batch:
        batch.drop_index("ix_partner_invoices_cancellation_number")
        batch.drop_constraint(
            "fk_partner_invoice_cancelled_by_admin", type_="foreignkey"
        )
        batch.drop_column("cancelled_by_admin_id")
        batch.drop_column("cancelled_at")
        batch.drop_column("cancellation_pdf_sha256")
        batch.drop_column("cancellation_document_version")
        batch.drop_column("cancellation_reason")
        batch.drop_column("cancellation_number")
