"""Journal local-to-S3 photo migration.

Revision ID: 0025_storage_journal
Revises: 0024_account_delete
"""

import sqlalchemy as sa
from alembic import op

revision = "0025_storage_journal"
down_revision = "0024_account_delete"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "storage_migration_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("photo_id", sa.Integer(), sa.ForeignKey("plant_photos.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source_path", sa.String(500), nullable=False),
        sa.Column("storage_key", sa.String(500), nullable=False),
        sa.Column("checksum_sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("photo_id", name="uq_storage_migration_photo"),
        sa.UniqueConstraint("storage_key", name="uq_storage_migration_key"),
    )
    op.create_index("ix_storage_migration_records_photo_id", "storage_migration_records", ["photo_id"])
    op.create_index("ix_storage_migration_records_status", "storage_migration_records", ["status"])


def downgrade() -> None:
    op.drop_table("storage_migration_records")
