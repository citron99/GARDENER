"""Вопросы, ответы, обратная связь и версии диагностики."""

import sqlalchemy as sa
from alembic import op

revision = "0003_diagnosis_workflow"
down_revision = "0002_diagnosis_photos"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("diagnoses", sa.Column("model_name", sa.String(120), nullable=False, server_default="mock-legacy"))
    op.add_column("diagnoses", sa.Column("prompt_version", sa.String(80), nullable=False, server_default="legacy-v1"))
    op.add_column("diagnoses", sa.Column("current_revision", sa.Integer(), nullable=False, server_default="1"))

    op.create_table(
        "diagnosis_revisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("diagnosis_id", sa.Integer(), sa.ForeignKey("diagnoses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("model_name", sa.String(120), nullable=False),
        sa.Column("prompt_version", sa.String(80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("diagnosis_id", "version", name="uq_diagnosis_revision_version"),
    )
    op.create_index("ix_diagnosis_revisions_diagnosis_id", "diagnosis_revisions", ["diagnosis_id"])
    op.execute(
        "INSERT INTO diagnosis_revisions "
        "(diagnosis_id, version, status, result, model_name, prompt_version, created_at) "
        "SELECT id, 1, status, result, model_name, prompt_version, created_at FROM diagnoses"
    )

    op.create_table(
        "diagnosis_questions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("diagnosis_id", sa.Integer(), sa.ForeignKey("diagnoses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("diagnosis_id", "revision_number", "position", name="uq_diagnosis_question_position"),
    )
    op.create_index("ix_diagnosis_questions_diagnosis_id", "diagnosis_questions", ["diagnosis_id"])

    op.create_table(
        "diagnosis_answers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("question_id", sa.Integer(), sa.ForeignKey("diagnosis_questions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_diagnosis_answers_question_id", "diagnosis_answers", ["question_id"], unique=True)

    op.create_table(
        "diagnosis_feedback",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("diagnosis_id", sa.Integer(), sa.ForeignKey("diagnoses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("helpful", sa.Boolean(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("rating BETWEEN 1 AND 5", name="ck_diagnosis_feedback_rating"),
    )
    op.create_index("ix_diagnosis_feedback_diagnosis_id", "diagnosis_feedback", ["diagnosis_id"])
    op.create_index("ix_diagnosis_feedback_user_id", "diagnosis_feedback", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_diagnosis_feedback_user_id", table_name="diagnosis_feedback")
    op.drop_index("ix_diagnosis_feedback_diagnosis_id", table_name="diagnosis_feedback")
    op.drop_table("diagnosis_feedback")
    op.drop_index("ix_diagnosis_answers_question_id", table_name="diagnosis_answers")
    op.drop_table("diagnosis_answers")
    op.drop_index("ix_diagnosis_questions_diagnosis_id", table_name="diagnosis_questions")
    op.drop_table("diagnosis_questions")
    op.drop_index("ix_diagnosis_revisions_diagnosis_id", table_name="diagnosis_revisions")
    op.drop_table("diagnosis_revisions")
    op.drop_column("diagnoses", "current_revision")
    op.drop_column("diagnoses", "prompt_version")
    op.drop_column("diagnoses", "model_name")
