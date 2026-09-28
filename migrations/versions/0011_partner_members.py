"""Partner staff accounts and isolated cabinet access."""

import sqlalchemy as sa
from alembic import op

revision = "0011_partner_members"
down_revision = "0010_partner_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "partner_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("partner_id", sa.Integer(), sa.ForeignKey("partners.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", name="uq_partner_member_user"),
        sa.UniqueConstraint("partner_id", "user_id", name="uq_partner_member_partner_user"),
    )
    op.create_index("ix_partner_members_partner_id", "partner_members", ["partner_id"])
    op.create_index("ix_partner_members_user_id", "partner_members", ["user_id"])
    op.create_index("ix_partner_members_active", "partner_members", ["active"])


def downgrade() -> None:
    op.drop_table("partner_members")