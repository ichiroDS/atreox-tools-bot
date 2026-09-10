"""Async engine / session factory.

PostgreSQL (asyncpg) is the production target; SQLite (aiosqlite) is the
zero-dependency local development target. The only difference lives here.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    kwargs: dict[str, Any] = {"echo": echo}
    if database_url.startswith("sqlite"):
        # aiosqlite has no server to ping, and a file database must tolerate
        # the handful of concurrent handler coroutines a local bot produces.
        kwargs["connect_args"] = {"timeout": 30}
    else:
        kwargs["pool_pre_ping"] = True
    return create_async_engine(database_url, **kwargs)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
