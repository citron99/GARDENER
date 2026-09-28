"""Partner catalog, products and privacy-minimal leads."""

import sqlalchemy as sa
from alembic import op

revision = "0010_partner_catalog"
down_revision = "0009_admin_role"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "partners",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(160), nullable=False, unique=True),
        sa.Column("website_url", sa.String(500), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_partners_active", "partners", ["active"])
    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("partner_id", sa.Integer(), sa.ForeignKey("partners.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("category", sa.String(40), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("product_url", sa.String(500), nullable=False),
        sa.Column("regions", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_products_partner_id", "products", ["partner_id"])
    op.create_index("ix_products_category", "products", ["category"])
    op.create_index("ix_products_active", "products", ["active"])
    op.create_table(
        "product_leads",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("diagnosis_id", sa.Integer(), sa.ForeignKey("diagnoses.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "product_id", "diagnosis_id", name="uq_product_lead_context"),
    )
    op.create_index("ix_product_leads_user_id", "product_leads", ["user_id"])
    op.create_index("ix_product_leads_product_id", "product_leads", ["product_id"])
    op.create_index("ix_product_leads_diagnosis_id", "product_leads", ["diagnosis_id"])


def downgrade() -> None:
    op.drop_table("product_leads")
    op.drop_table("products")
    op.drop_table("partners")