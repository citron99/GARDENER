"""Add stable taxonomy codes to plants and product recommendation rules.

Revision ID: 0029_catalog_taxonomy
Revises: 0028_photo_quotas
"""

import sqlalchemy as sa
from alembic import op

revision = "0029_catalog_taxonomy"
down_revision = "0028_photo_quotas"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("plants") as batch:
        batch.add_column(sa.Column("taxon_id", sa.String(length=100), nullable=True))
        batch.create_index("ix_plants_taxon_id", ["taxon_id"], unique=False)
    with op.batch_alter_table("product_recommendation_rules") as batch:
        batch.add_column(sa.Column("plant_taxon_id", sa.String(length=100), nullable=True))
        batch.add_column(sa.Column("problem_code", sa.String(length=80), nullable=True))
        batch.add_column(sa.Column("country_code", sa.String(length=2), nullable=True))
        batch.create_index(
            "ix_product_recommendation_rules_plant_taxon_id", ["plant_taxon_id"], unique=False)
        batch.create_index(
            "ix_product_recommendation_rules_problem_code", ["problem_code"], unique=False)
        batch.create_index(
            "ix_product_recommendation_rules_country_code", ["country_code"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("product_recommendation_rules") as batch:
        batch.drop_index("ix_product_recommendation_rules_country_code")
        batch.drop_index("ix_product_recommendation_rules_problem_code")
        batch.drop_index("ix_product_recommendation_rules_plant_taxon_id")
        batch.drop_column("country_code")
        batch.drop_column("problem_code")
        batch.drop_column("plant_taxon_id")
    with op.batch_alter_table("plants") as batch:
        batch.drop_index("ix_plants_taxon_id")
        batch.drop_column("taxon_id")
