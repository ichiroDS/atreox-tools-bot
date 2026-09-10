"""Production startup steps: reach the database, then bring the schema up.

Kept out of ``app.main`` so the bot process itself stays a single concern and
can still be launched directly with ``python -m app.main``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from sqlalchemy import text

from app.db.session import create_engine

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def describe_database(database_url: str) -> str:
    """The URL with its credentials removed, safe for logs."""
    if "://" not in database_url:
        return database_url
    scheme, rest = database_url.split("://", 1)
    # Anything before the last '@' is user:password.
    host = rest.rsplit("@", 1)[-1] if "@" in rest else rest
    return f"{scheme}://{host}"


async def wait_for_database(
    database_url: str,
    *,
    attempts: int = 30,
    delay: float = 2.0,
) -> None:
    """Block until the database answers, or give up after ``attempts`` tries.

    A managed PostgreSQL instance is often still accepting connections a few
    seconds after the container starts, so a plain connect-on-boot would crash
    the first deploy of every release.
    """
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        engine = create_engine(database_url)
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            logger.info("database connected (%s)", describe_database(database_url))
            return
        except Exception as exc:  # noqa: BLE001 - any driver error means retry
            last_error = exc
            logger.warning(
                "database not ready yet (attempt %d/%d): %s",
                attempt,
                attempts,
                type(exc).__name__,
            )
            await asyncio.sleep(delay)
        finally:
            await engine.dispose()

    raise RuntimeError(
        f"database unreachable after {attempts} attempts: {type(last_error).__name__}"
    ) from last_error


def run_migrations(database_url: str) -> None:
    """Run ``alembic upgrade head``.

    Concurrency is handled inside ``alembic/env.py``: on PostgreSQL the
    migration takes an advisory lock, so two instances booting together cannot
    apply the same revision twice.
    """
    from alembic import command
    from alembic.config import Config

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    # env.py falls back to settings when this is unset; being explicit keeps the
    # entrypoint and the application pointed at exactly the same database.
    config.set_main_option("sqlalchemy.url", database_url)

    logger.info("running migrations")
    command.upgrade(config, "head")
    logger.info("migrations complete")


async def prepare_database(database_url: str, *, attempts: int = 30) -> None:
    """Wait for the database, then migrate it."""
    await wait_for_database(database_url, attempts=attempts)
    # Alembic's API is synchronous and opens its own connection; run it off the
    # event loop so the async engine above is fully disposed first.
    await asyncio.to_thread(run_migrations, database_url)
