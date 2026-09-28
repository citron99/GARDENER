"""Persistent background diagnosis jobs."""

import sqlalchemy as sa
from alembic import op

revision = "0008_diagnosis_jobs"
down_revision = "0007_reminders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "diagnosis_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plant_id", sa.Integer(), sa.ForeignKey("plants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("diagnosis_id", sa.Integer(), sa.ForeignKey("diagnoses.id", ondelete="SET NULL"), nullable=True),
        sa.Column("celery_task_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_type", sa.String(80), nullable=True),
        sa.Column("error_message", sa.String(300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_diagnosis_jobs_user_id", "diagnosis_jobs", ["user_id"])
    op.create_index("ix_diagnosis_jobs_plant_id", "diagnosis_jobs", ["plant_id"])
    op.create_index("ix_diagnosis_jobs_diagnosis_id", "diagnosis_jobs", ["diagnosis_id"])
    op.create_index("ix_diagnosis_jobs_status", "diagnosis_jobs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_diagnosis_jobs_status", table_name="diagnosis_jobs")
    op.drop_index("ix_diagnosis_jobs_diagnosis_id", table_name="diagnosis_jobs")
    op.drop_index("ix_diagnosis_jobs_plant_id", table_name="diagnosis_jobs")
    op.drop_index("ix_diagnosis_jobs_user_id", table_name="diagnosis_jobs")
    op.drop_table("diagnosis_jobs")