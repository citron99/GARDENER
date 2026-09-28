"""object storage keys for private photos

Revision ID: 0018_object_storage
Revises: 0017_auth_sessions
"""

import sqlalchemy as sa
from alembic import op

revision = "0018_object_storage"
down_revision = "0017_auth_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("plant_photos") as batch:
        batch.alter_column("file_path", existing_type=sa.String(500), nullable=True)
        batch.add_column(sa.Column("storage_key", sa.String(500), nullable=True))
        batch.create_index("ix_plant_photos_storage_key", ["storage_key"], unique=True)
    op.execute("UPDATE plant_photos SET storage_key = file_path WHERE storage_key IS NULL")


def downgrade() -> None:
    op.execute("UPDATE plant_photos SET file_path = storage_key WHERE file_path IS NULL")
    with op.batch_alter_table("plant_photos") as batch:
        batch.drop_index("ix_plant_photos_storage_key")
        batch.drop_column("storage_key")
        batch.alter_column("file_path", existing_type=sa.String(500), nullable=False)
