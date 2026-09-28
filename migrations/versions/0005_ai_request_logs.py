"""Аудит AI-запросов, токенов, задержки и ошибок."""

import sqlalchemy as sa
from alembic import op

revision = "0005_ai_request_logs"
down_revision = "0004_unique_feedback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_request_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("diagnosis_id", sa.Integer(), sa.ForeignKey("diagnoses.id", ondelete="SET NULL"), nullable=True),
        sa.Column("request_id", sa.String(120), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("model_name", sa.String(120), nullable=False),
        sa.Column("prompt_version", sa.String(80), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("error_type", sa.String(80), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("response_ms", sa.Integer(), nullable=False),
        sa.Column("estimated_cost", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_ai_request_logs_user_id", "ai_request_logs", ["user_id"])
    op.create_index("ix_ai_request_logs_diagnosis_id", "ai_request_logs", ["diagnosis_id"])
    op.create_index("ix_ai_request_logs_request_id", "ai_request_logs", ["request_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_ai_request_logs_request_id", table_name="ai_request_logs")
    op.drop_index("ix_ai_request_logs_diagnosis_id", table_name="ai_request_logs")
    op.drop_index("ix_ai_request_logs_user_id", table_name="ai_request_logs")
    op.drop_table("ai_request_logs")