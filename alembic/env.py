"""Alembic environment - the database URL always comes from app settings."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import text

from alembic import context
from app.config import get_settings
from app.db.session import create_engine
from app.db.models import Base  # noqa: F401 - imports every model into metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations_offline() -> None:
    url = _database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=url.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        # SQLite cannot ALTER most things in place; batch mode rewrites the
        # table instead. No-op for PostgreSQL.
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


# Arbitrary but fixed: every instance of this app must agree on the number.
_MIGRATION_LOCK_KEY = 8899624669


async def run_migrations_online() -> None:
    engine = create_engine(_database_url())
    async with engine.connect() as connection:
        # Two containers booting together would otherwise race to apply the
        # same revision. The lock is session scoped and held for the migration.
        locked = connection.dialect.name == "postgresql"
        if locked:
            await connection.execute(
                text("SELECT pg_advisory_lock(:key)"), {"key": _MIGRATION_LOCK_KEY}
            )
        try:
            await connection.run_sync(_run_migrations)
            # SQLite has no transactional DDL, so alembic leaves the commit to us.
            await connection.commit()
        finally:
            if locked:
                await connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": _MIGRATION_LOCK_KEY},
                )
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
