"""Add localized aliases for stable catalog taxonomy codes.

Revision ID: 0031_taxonomy_aliases
Revises: 0030_knowledge_review
"""

import sqlalchemy as sa
from alembic import op

revision = "0031_taxonomy_aliases"
down_revision = "0030_knowledge_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "catalog_taxonomy_aliases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("alias_type", sa.String(length=20), nullable=False),
        sa.Column("stable_code", sa.String(length=100), nullable=False),
        sa.Column("locale", sa.String(length=5), nullable=False, server_default="*"),
        sa.Column("alias", sa.String(length=200), nullable=False),
        sa.Column("normalized_alias", sa.String(length=200), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("alias_type", "locale", "normalized_alias",
                            name="uq_catalog_taxonomy_alias"),
    )
    for column in ("alias_type", "stable_code", "locale", "normalized_alias", "active"):
        op.create_index(f"ix_catalog_taxonomy_aliases_{column}",
                        "catalog_taxonomy_aliases", [column])


def downgrade() -> None:
    op.drop_table("catalog_taxonomy_aliases")
