from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app import models  # noqa: F401 - registers ORM metadata
from app.config import settings
from app.database import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

target_metadata = Base.metadata


def compare_column_type(_context, _inspected_column, _metadata_column, inspected_type, metadata_type):
    """Avoid perpetual Alembic diffs for pgvector Vector(N) columns."""
    from app.vector import Vector

    if not isinstance(metadata_type, Vector):
        return None
    inspected_dimensions = getattr(inspected_type, "dim", None) or getattr(inspected_type, "dimensions", None)
    if inspected_dimensions is not None:
        return int(inspected_dimensions) != metadata_type.dimensions
    normalized = str(inspected_type).upper().replace(" ", "")
    return normalized not in {"VECTOR", f"VECTOR({metadata_type.dimensions})"}


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=compare_column_type,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=compare_column_type)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
