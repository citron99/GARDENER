"""Связь диагностик с использованными фотографиями."""

import sqlalchemy as sa
from alembic import op

revision = "0002_diagnosis_photos"
down_revision = "0001_base"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "diagnosis_photos",
        sa.Column("diagnosis_id", sa.Integer(), sa.ForeignKey("diagnoses.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("photo_id", sa.Integer(), sa.ForeignKey("plant_photos.id", ondelete="CASCADE"), primary_key=True),
    )


def downgrade() -> None:
    op.drop_table("diagnosis_photos")
