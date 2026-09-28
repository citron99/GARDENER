"""partner conversion attribution and invoices

Revision ID: 0021_partner_billing
Revises: 0020_email_security
"""

import sqlalchemy as sa
from alembic import op

revision = "0021_partner_billing"
down_revision = "0020_email_security"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("partners") as batch:
        batch.add_column(sa.Column("billing_plan", sa.String(30), nullable=False, server_default="free"))
        batch.add_column(sa.Column("monthly_fee_cents", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("confirmed_lead_price_cents", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("billing_currency", sa.String(3), nullable=False, server_default="eur"))
        batch.create_index("ix_partners_billing_plan", ["billing_plan"])

    op.create_table(
        "partner_invoices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("partner_id", sa.Integer(), sa.ForeignKey("partners.id", ondelete="CASCADE"), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="eur"),
        sa.Column("monthly_fee_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("confirmed_leads_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lead_fees_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="issued"),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("partner_id", "period_start", "period_end", "status"):
        op.create_index(f"ix_partner_invoices_{column}", "partner_invoices", [column])

    with op.batch_alter_table("product_leads") as batch:
        batch.add_column(sa.Column("status", sa.String(30), nullable=False, server_default="clicked"))
        batch.add_column(sa.Column("partner_reference", sa.String(120), nullable=True))
        batch.add_column(sa.Column("conversion_value_cents", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("review_note", sa.String(500), nullable=True))
        batch.add_column(sa.Column("reviewed_by_admin_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("billable_amount_cents", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("invoice_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_product_leads_reviewed_admin", "users", ["reviewed_by_admin_id"], ["id"], ondelete="SET NULL")
        batch.create_foreign_key("fk_product_leads_invoice", "partner_invoices", ["invoice_id"], ["id"], ondelete="SET NULL")
        batch.create_unique_constraint("uq_product_lead_partner_reference", ["product_id", "partner_reference"])
        for column in ("status", "claimed_at", "confirmed_at", "invoice_id"):
            batch.create_index(f"ix_product_leads_{column}", [column])


def downgrade() -> None:
    with op.batch_alter_table("product_leads") as batch:
        for column in ("invoice_id", "confirmed_at", "claimed_at", "status"):
            batch.drop_index(f"ix_product_leads_{column}")
        batch.drop_constraint("uq_product_lead_partner_reference", type_="unique")
        batch.drop_constraint("fk_product_leads_invoice", type_="foreignkey")
        batch.drop_constraint("fk_product_leads_reviewed_admin", type_="foreignkey")
        for column in (
            "invoice_id", "billable_amount_cents", "reviewed_by_admin_id", "review_note",
            "rejected_at", "confirmed_at", "claimed_at", "conversion_value_cents",
            "partner_reference", "status",
        ):
            batch.drop_column(column)

    op.drop_table("partner_invoices")
    with op.batch_alter_table("partners") as batch:
        batch.drop_index("ix_partners_billing_plan")
        for column in ("billing_currency", "confirmed_lead_price_cents", "monthly_fee_cents", "billing_plan"):
            batch.drop_column(column)
