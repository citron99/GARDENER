"""Add database-level integrity constraints for commerce records.

Revision ID: 0039_commerce_constraints
Revises: 0038_invoice_pdf_storage
"""

from alembic import op

revision = "0039_commerce_constraints"
down_revision = "0038_invoice_pdf_storage"
branch_labels = None
depends_on = None


CONSTRAINTS = {
    "partners": (
        ("ck_partner_monthly_fee_nonnegative", "monthly_fee_cents >= 0"),
        (
            "ck_partner_lead_price_nonnegative",
            "confirmed_lead_price_cents >= 0",
        ),
        (
            "ck_partner_currency_iso",
            "length(billing_currency) = 3 AND billing_currency = lower(billing_currency)",
        ),
    ),
    "products": (
        (
            "ck_product_price_nonnegative",
            "price_cents IS NULL OR price_cents >= 0",
        ),
        (
            "ck_product_currency_iso",
            "length(currency) = 3 AND currency = lower(currency)",
        ),
        (
            "ck_product_moderation_status",
            "moderation_status IN ('draft', 'pending', 'approved', 'rejected')",
        ),
    ),
    "partner_invoices": (
        ("ck_invoice_period", "period_start <= period_end"),
        ("ck_invoice_monthly_fee_nonnegative", "monthly_fee_cents >= 0"),
        ("ck_invoice_lead_count_nonnegative", "confirmed_leads_count >= 0"),
        ("ck_invoice_lead_fees_nonnegative", "lead_fees_cents >= 0"),
        ("ck_invoice_total_nonnegative", "total_cents >= 0"),
        (
            "ck_invoice_total_consistent",
            "total_cents = monthly_fee_cents + lead_fees_cents",
        ),
        (
            "ck_invoice_currency_iso",
            "length(currency) = 3 AND currency = lower(currency)",
        ),
        ("ck_invoice_status", "status IN ('issued', 'paid', 'void')"),
    ),
    "partner_invoice_deliveries": (
        ("ck_invoice_delivery_attempt_positive", "attempt_number > 0"),
        (
            "ck_invoice_delivery_status",
            "status IN ('pending', 'sending', 'sent', 'logged', 'failed', 'unknown')",
        ),
    ),
    "partner_payments": (
        ("ck_partner_payment_amount_positive", "amount_cents > 0"),
        (
            "ck_partner_payment_currency_iso",
            "length(currency) = 3 AND currency = lower(currency)",
        ),
        (
            "ck_partner_payment_status",
            "status IN ('matched', 'unmatched', 'rejected')",
        ),
    ),
}


def upgrade() -> None:
    for table_name, constraints in CONSTRAINTS.items():
        with op.batch_alter_table(table_name) as batch:
            for name, condition in constraints:
                batch.create_check_constraint(name, condition)


def downgrade() -> None:
    for table_name, constraints in reversed(tuple(CONSTRAINTS.items())):
        with op.batch_alter_table(table_name) as batch:
            for name, _condition in reversed(constraints):
                batch.drop_constraint(name, type_="check")
