"""Один отзыв пользователя на одну диагностику."""

from alembic import op

revision = "0004_unique_feedback"
down_revision = "0003_diagnosis_workflow"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "DELETE FROM diagnosis_feedback WHERE id NOT IN ("
        "SELECT MAX(id) FROM diagnosis_feedback GROUP BY diagnosis_id, user_id)"
    )
    with op.batch_alter_table("diagnosis_feedback") as batch_op:
        batch_op.create_unique_constraint("uq_diagnosis_feedback_user", ["diagnosis_id", "user_id"])


def downgrade() -> None:
    with op.batch_alter_table("diagnosis_feedback") as batch_op:
        batch_op.drop_constraint("uq_diagnosis_feedback_user", type_="unique")
