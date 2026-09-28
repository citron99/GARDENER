"""reconcile the migrated schema with the ORM models

Revision ID: 0040_schema_model_drift
Revises: 0039_commerce_constraints

The models express one-row-per-user uniqueness as a unique *index*
(`index=True, unique=True` on a column), while several earlier migrations
additionally created a unique *constraint* on the same column. That
duplication is what `alembic check` reports as drift. Dropping the extra
constraints is safe: the unique index on the same column stays in place and
keeps enforcing uniqueness. Two indexes that the migrations created without
`unique=True` are recreated as unique so that they match the models too.

Keep the revision id under 33 characters: alembic stores it in the
VARCHAR(32) `alembic_version.version_num` column (migration 0016 widens it
because its own id did not fit).
"""

from alembic import op

revision = "0040_schema_model_drift"
down_revision = "0039_commerce_constraints"
branch_labels = None
depends_on = None

# (table, redundant unique constraint, columns) - each column also carries a
# unique index, so uniqueness stays enforced after the constraint is dropped.
_REDUNDANT_UNIQUE_CONSTRAINTS = (
    ("account_deletion_requests", "uq_account_deletion_request_user", ["user_id"]),
    ("billing_events", "billing_events_provider_event_id_key", ["provider_event_id"]),
    ("storage_migration_records", "uq_storage_migration_photo", ["photo_id"]),
    ("subscriptions", "subscriptions_user_id_key", ["user_id"]),
    ("telegram_accounts", "telegram_accounts_chat_id_key", ["chat_id"]),
    ("telegram_accounts", "telegram_accounts_user_id_key", ["user_id"]),
    ("telegram_link_tokens", "telegram_link_tokens_token_hash_key", ["token_hash"]),
    ("telegram_updates", "telegram_updates_update_id_key", ["update_id"]),
    ("user_notifications", "user_notifications_deduplication_key_key", ["deduplication_key"]),
)

# (table, index, columns): the models declare these indexes as unique.
_INDEXES_TO_MAKE_UNIQUE = (
    ("account_deletion_requests", "ix_account_deletion_requests_user_id", ["user_id"]),
    ("storage_migration_records", "ix_storage_migration_records_photo_id", ["photo_id"]),
)


def _postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    for table, index, columns in _INDEXES_TO_MAKE_UNIQUE:
        op.drop_index(index, table_name=table)
        op.create_index(index, table, columns, unique=True)
    if not _postgresql():
        # SQLite stores an anonymous table-level UNIQUE as an auto-index and
        # cannot alter constraints, so the harmless duplicate stays there.
        return
    for table, constraint, _columns in _REDUNDANT_UNIQUE_CONSTRAINTS:
        op.drop_constraint(constraint, table, type_="unique")


def downgrade() -> None:
    if _postgresql():
        for table, constraint, columns in reversed(_REDUNDANT_UNIQUE_CONSTRAINTS):
            op.create_unique_constraint(constraint, table, columns)
    for table, index, columns in reversed(_INDEXES_TO_MAKE_UNIQUE):
        op.drop_index(index, table_name=table)
        op.create_index(index, table, columns, unique=False)
