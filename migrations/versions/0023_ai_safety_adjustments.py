"""Persistent audit of deterministic AI safety corrections.

Revision ID: 0023_ai_safety
Revises: 0022_product_registry
"""

import sqlalchemy as sa
from alembic import op

revision = "0023_ai_safety"
down_revision = "0022_product_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_safety_adjustments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("diagnosis_id", sa.Integer(), sa.ForeignKey("diagnoses.id", ondelete="SET NULL"), nullable=True),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("diagnosis_jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("removed_action_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("user_id", "diagnosis_id", "job_id", "created_at"):
        op.create_index(f"ix_ai_safety_adjustments_{column}", "ai_safety_adjustments", [column])


def downgrade() -> None:
    op.drop_table("ai_safety_adjustments")
