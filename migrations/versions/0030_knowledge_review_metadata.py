"""Add review expiry and provenance metadata to knowledge sources.

Revision ID: 0030_knowledge_review
Revises: 0029_catalog_taxonomy
"""

import sqlalchemy as sa
from alembic import op

revision = "0030_knowledge_review"
down_revision = "0029_catalog_taxonomy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("knowledge_sources") as batch:
        batch.add_column(sa.Column("next_review_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("review_role", sa.String(length=30), nullable=False,
                                   server_default="editorial"))
        batch.add_column(sa.Column("usage_basis", sa.String(length=80), nullable=False,
                                   server_default="linked_factual_summary"))
    op.execute("UPDATE knowledge_sources SET next_review_at = last_verified_at WHERE next_review_at IS NULL")
    with op.batch_alter_table("knowledge_sources") as batch:
        batch.alter_column("next_review_at", nullable=False)
        batch.create_index("ix_knowledge_sources_next_review_at", ["next_review_at"], unique=False)
        batch.create_index("ix_knowledge_sources_review_role", ["review_role"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("knowledge_sources") as batch:
        batch.drop_index("ix_knowledge_sources_review_role")
        batch.drop_index("ix_knowledge_sources_next_review_at")
        batch.drop_column("usage_basis")
        batch.drop_column("review_role")
        batch.drop_column("next_review_at")
