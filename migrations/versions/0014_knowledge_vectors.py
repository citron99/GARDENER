"""Managed verified knowledge sources and vector chunks."""

import sqlalchemy as sa
from alembic import op

from app.config import settings
from app.vector import Vector

revision = "0014_knowledge_vectors"
down_revision = "0013_telegram"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "knowledge_sources",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("url", sa.String(1_000), nullable=False, unique=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("keywords", sa.JSON(), nullable=False),
        sa.Column("region", sa.String(30), nullable=False),
        sa.Column("languages", sa.JSON(), nullable=False),
        sa.Column("plant_types", sa.JSON(), nullable=False),
        sa.Column("problem_types", sa.JSON(), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_by", sa.String(160), nullable=False),
        sa.Column("source_version", sa.String(100), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_knowledge_sources_region", "knowledge_sources", ["region"])
    op.create_index("ix_knowledge_sources_active", "knowledge_sources", ["active"])
    op.create_index("ix_knowledge_sources_last_verified_at", "knowledge_sources", ["last_verified_at"])
    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.String(100), sa.ForeignKey("knowledge_sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding_model", sa.String(100), nullable=False),
        sa.Column("embedding", Vector(settings.knowledge_embedding_dimensions), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_id", "position", name="uq_knowledge_chunk_position"),
    )
    op.create_index("ix_knowledge_chunks_source_id", "knowledge_chunks", ["source_id"])
    if bind.dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX ix_knowledge_chunks_embedding_hnsw ON knowledge_chunks "
            "USING hnsw (embedding vector_cosine_ops)"
        )


def downgrade() -> None:
    op.drop_table("knowledge_chunks")
    op.drop_table("knowledge_sources")