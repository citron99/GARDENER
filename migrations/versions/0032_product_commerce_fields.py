"""Add partner SKU, price, stock, and image metadata.

Revision ID: 0032_product_commerce
Revises: 0031_taxonomy_aliases
"""

import sqlalchemy as sa
from alembic import op

revision = "0032_product_commerce"
down_revision = "0031_taxonomy_aliases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("products") as batch:
        batch.add_column(sa.Column("sku", sa.String(length=100), nullable=True))
        batch.add_column(sa.Column("image_url", sa.String(length=500), nullable=True))
        batch.add_column(sa.Column("price_cents", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("currency", sa.String(length=3), nullable=False,
                                   server_default="eur"))
        batch.add_column(sa.Column("in_stock", sa.Boolean(), nullable=False,
                                   server_default=sa.true()))
        batch.create_index("ix_products_sku", ["sku"], unique=False)
        batch.create_index("ix_products_in_stock", ["in_stock"], unique=False)
        batch.create_unique_constraint("uq_product_partner_sku", ["partner_id", "sku"])


def downgrade() -> None:
    with op.batch_alter_table("products") as batch:
        batch.drop_constraint("uq_product_partner_sku", type_="unique")
        batch.drop_index("ix_products_in_stock")
        batch.drop_index("ix_products_sku")
        batch.drop_column("in_stock")
        batch.drop_column("currency")
        batch.drop_column("price_cents")
        batch.drop_column("image_url")
        batch.drop_column("sku")
