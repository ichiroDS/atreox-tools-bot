"""Production startup: reaching the database, migrating, and leaking nothing."""

from __future__ import annotations

import pytest

from app.startup import describe_database, prepare_database, wait_for_database


# --- credential safety ------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "postgresql+asyncpg://atreox:sup3rs3cret@db.internal:5432/atreox",
            "postgresql+asyncpg://db.internal:5432/atreox",
        ),
        (
            "postgresql+asyncpg://user@host/db",
            "postgresql+asyncpg://host/db",
        ),
        (
            "sqlite+aiosqlite:///./atreox_tools.db",
            "sqlite+aiosqlite:///./atreox_tools.db",
        ),
        ("not-a-url", "not-a-url"),
    ],
)
def test_describe_database_strips_credentials(url, expected):
    assert describe_database(url) == expected


def test_a_password_containing_an_at_sign_is_still_removed():
    described = describe_database("postgresql+asyncpg://u:p@ss@host:5432/db")
    assert "p@ss" not in described
    assert described.endswith("host:5432/db")


# --- waiting for the database ----------------------------------------------


async def test_wait_returns_as_soon_as_the_database_answers(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'ready.db'}"
    await wait_for_database(url, attempts=1, delay=0)


async def test_wait_retries_then_gives_up_with_a_clear_error():
    # A driver that is not installed fails every attempt.
    with pytest.raises(RuntimeError, match="unreachable after 2 attempts"):
        await wait_for_database(
            "postgresql+asyncpg://user:pw@127.0.0.1:1/none", attempts=2, delay=0
        )


async def test_the_failure_message_never_contains_the_password():
    with pytest.raises(RuntimeError) as excinfo:
        await wait_for_database(
            "postgresql+asyncpg://user:hunter2@127.0.0.1:1/none", attempts=1, delay=0
        )
    assert "hunter2" not in str(excinfo.value)


# --- migrating --------------------------------------------------------------


async def test_prepare_database_brings_an_empty_database_to_head(tmp_path, monkeypatch):
    """The whole entrypoint sequence against a database that does not exist."""
    database = tmp_path / "fresh.db"
    url = f"sqlite+aiosqlite:///{database}"
    monkeypatch.setenv("DATABASE_URL", url)

    await prepare_database(url, attempts=1)

    import sqlite3

    connection = sqlite3.connect(database)
    tables = {
        row[0]
        for row in connection.execute(
            "select name from sqlite_master where type='table'"
        )
    }
    assert {"users", "jobs", "sticker_sets", "sticker_samples", "feature_events"} <= tables
    # Schema is at the latest revision, and the CTA columns from 0002 exist.
    revision = connection.execute("select version_num from alembic_version").fetchone()
    assert revision == ("0002",)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
    assert {"cta_shown_count", "cta_last_shown_at"} <= columns
    connection.close()


async def test_running_the_sequence_twice_is_safe(tmp_path, monkeypatch):
    """A redeploy re-runs the entrypoint against an already migrated database."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'twice.db'}"
    monkeypatch.setenv("DATABASE_URL", url)

    await prepare_database(url, attempts=1)
    await prepare_database(url, attempts=1)
