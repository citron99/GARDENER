"""Detect reuse of a rotated refresh token.

Revision ID: 0027_refresh_reuse
Revises: 0026_partner_clicks
"""

import sqlalchemy as sa
from alembic import op

revision = "0027_refresh_reuse"
down_revision = "0026_partner_clicks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("auth_sessions") as batch:
        batch.add_column(sa.Column("previous_refresh_token_hash", sa.String(64), nullable=True))
        batch.create_index(
            "ix_auth_sessions_previous_refresh_token_hash",
            ["previous_refresh_token_hash"],
            unique=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("auth_sessions") as batch:
        batch.drop_index("ix_auth_sessions_previous_refresh_token_hash")
        batch.drop_column("previous_refresh_token_hash")
