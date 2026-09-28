"""Базовая схема MVP до связи диагностик с фотографиями."""

import sqlalchemy as sa
from alembic import op

revision = "0001_base"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("language", sa.String(5), nullable=False),
        sa.Column("region", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "gardens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("location", sa.String(160), nullable=True),
        sa.Column("soil_type", sa.String(120), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "name", name="uq_garden_user_name"),
    )
    op.create_index("ix_gardens_user_id", "gardens", ["user_id"])

    op.create_table(
        "plants",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("garden_id", sa.Integer(), sa.ForeignKey("gardens.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("species", sa.String(160), nullable=True),
        sa.Column("growing_place", sa.String(40), nullable=False),
        sa.Column("region", sa.String(120), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_plants_garden_id", "plants", ["garden_id"])

    op.create_table(
        "plant_photos",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("plant_id", sa.Integer(), sa.ForeignKey("plants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("file_path", sa.String(500), nullable=False),
        sa.Column("content_type", sa.String(50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_plant_photos_plant_id", "plant_photos", ["plant_id"])

    op.create_table(
        "diagnoses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("plant_id", sa.Integer(), sa.ForeignKey("plants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("symptoms", sa.Text(), nullable=False),
        sa.Column("damaged_part", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_diagnoses_plant_id", "diagnoses", ["plant_id"])


def downgrade() -> None:
    op.drop_index("ix_diagnoses_plant_id", table_name="diagnoses")
    op.drop_table("diagnoses")
    op.drop_index("ix_plant_photos_plant_id", table_name="plant_photos")
    op.drop_table("plant_photos")
    op.drop_index("ix_plants_garden_id", table_name="plants")
    op.drop_table("plants")
    op.drop_index("ix_gardens_user_id", table_name="gardens")
    op.drop_table("gardens")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
