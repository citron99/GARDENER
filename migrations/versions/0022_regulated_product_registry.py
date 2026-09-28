"""regulated product registration snapshots

Revision ID: 0022_product_registry
Revises: 0021_partner_billing
"""

import sqlalchemy as sa
from alembic import op

revision = "0022_product_registry"
down_revision = "0021_partner_billing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "regulated_product_registrations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_name", sa.String(40), nullable=False),
        sa.Column("jurisdiction", sa.String(2), nullable=False),
        sa.Column("registration_number", sa.String(100), nullable=False),
        sa.Column("product_name", sa.String(300), nullable=False),
        sa.Column("holder_name", sa.String(300), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column("source_url", sa.String(1000), nullable=False),
        sa.Column("source_version", sa.String(120), nullable=False),
        sa.Column("record_checksum", sa.String(64), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("jurisdiction", "registration_number", name="uq_regulated_registration_number"),
    )
    for column in ("source_name", "jurisdiction", "registration_number", "status", "valid_until", "last_synced_at"):
        op.create_index(
            f"ix_regulated_product_registrations_{column}",
            "regulated_product_registrations",
            [column],
        )
    with op.batch_alter_table("product_recommendation_rules") as batch:
        batch.add_column(sa.Column("registry_entry_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_product_rules_registry_entry", "regulated_product_registrations",
            ["registry_entry_id"], ["id"], ondelete="SET NULL",
        )
        batch.create_index("ix_product_recommendation_rules_registry_entry_id", ["registry_entry_id"])


def downgrade() -> None:
    with op.batch_alter_table("product_recommendation_rules") as batch:
        batch.drop_index("ix_product_recommendation_rules_registry_entry_id")
        batch.drop_constraint("fk_product_rules_registry_entry", type_="foreignkey")
        batch.drop_column("registry_entry_id")
    op.drop_table("regulated_product_registrations")
