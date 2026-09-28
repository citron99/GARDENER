"""Store immutable invoice and cancellation PDF objects.

Revision ID: 0038_invoice_pdf_storage
Revises: 0037_invoice_cancellations
"""

import sqlalchemy as sa
from alembic import op

revision = "0038_invoice_pdf_storage"
down_revision = "0037_invoice_cancellations"
branch_labels = None
depends_on = None


INVOICE_COLUMNS = (
    sa.Column("snapshot_sha256", sa.String(length=64), nullable=True),
    sa.Column("pdf_storage_backend", sa.String(length=20), nullable=True),
    sa.Column("pdf_storage_key", sa.String(length=700), nullable=True),
    sa.Column("pdf_file_path", sa.String(length=1000), nullable=True),
    sa.Column("pdf_size_bytes", sa.Integer(), nullable=True),
    sa.Column("pdf_content_type", sa.String(length=100), nullable=True),
    sa.Column("pdf_generator_version", sa.String(length=160), nullable=True),
    sa.Column("cancellation_storage_backend", sa.String(length=20), nullable=True),
    sa.Column("cancellation_snapshot_sha256", sa.String(length=64), nullable=True),
    sa.Column("cancellation_storage_key", sa.String(length=700), nullable=True),
    sa.Column("cancellation_file_path", sa.String(length=1000), nullable=True),
    sa.Column("cancellation_size_bytes", sa.Integer(), nullable=True),
    sa.Column("cancellation_content_type", sa.String(length=100), nullable=True),
    sa.Column("cancellation_generator_version", sa.String(length=160), nullable=True),
)


def upgrade() -> None:
    with op.batch_alter_table("partner_invoices") as batch:
        for column in INVOICE_COLUMNS:
            batch.add_column(column)


def downgrade() -> None:
    with op.batch_alter_table("partner_invoices") as batch:
        for column in reversed(INVOICE_COLUMNS):
            batch.drop_column(column.name)
