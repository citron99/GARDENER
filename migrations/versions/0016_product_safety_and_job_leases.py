"""product safety rules and diagnosis job leases

Revision ID: 0016
Revises: 0015
"""

import sqlalchemy as sa
from alembic import op

revision = "0016_product_safety_and_job_leases"
down_revision = "0015_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("products") as batch:
        batch.add_column(sa.Column("moderation_status", sa.String(20), nullable=False, server_default="draft"))
        batch.add_column(sa.Column("moderation_note", sa.String(500), nullable=True))
        batch.add_column(sa.Column("moderated_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_index("ix_products_moderation_status", ["moderation_status"])
    op.create_table(
        "product_recommendation_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("crop_name", sa.String(160), nullable=False),
        sa.Column("problem_name", sa.String(200), nullable=False),
        sa.Column("safe_action", sa.String(500), nullable=False),
        sa.Column("region_code", sa.String(20), nullable=False),
        sa.Column("registration_country", sa.String(2), nullable=True),
        sa.Column("registration_number", sa.String(100), nullable=True),
        sa.Column("registration_url", sa.String(500), nullable=True),
        sa.Column("registration_expires_on", sa.Date(), nullable=True),
        sa.Column("expert_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("product_id", "crop_name", "problem_name", "region_code", name="uq_product_recommendation_context"),
    )
    for column in ("product_id", "crop_name", "problem_name", "region_code", "registration_country",
                   "registration_expires_on", "expert_verified", "active"):
        op.create_index(f"ix_product_recommendation_rules_{column}", "product_recommendation_rules", [column])

    with op.batch_alter_table("diagnosis_jobs") as batch:
        batch.add_column(sa.Column("operation", sa.String(20), nullable=False, server_default="create"))
        batch.add_column(sa.Column("execution_token", sa.String(36), nullable=True))
        batch.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_index("ix_diagnosis_jobs_execution_token", ["execution_token"], unique=True)
        batch.create_index("ix_diagnosis_jobs_lease_expires_at", ["lease_expires_at"])


def downgrade() -> None:
    with op.batch_alter_table("diagnosis_jobs") as batch:
        batch.drop_index("ix_diagnosis_jobs_lease_expires_at")
        batch.drop_index("ix_diagnosis_jobs_execution_token")
        batch.drop_column("lease_expires_at")
        batch.drop_column("execution_token")
        batch.drop_column("operation")
    op.drop_table("product_recommendation_rules")
    with op.batch_alter_table("products") as batch:
        batch.drop_index("ix_products_moderation_status")
        batch.drop_column("moderated_at")
        batch.drop_column("moderation_note")
        batch.drop_column("moderation_status")
