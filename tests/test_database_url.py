"""DATABASE_URL normalisation.

Managed platforms hand out libpq-style URLs. Everything here goes through
``create_async_engine``, so a URL naming a synchronous driver - or naming none,
which means psycopg2 - has to be rewritten before it reaches the engine.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import LOCAL_SQLITE_URL, Settings, normalise_database_url

# The shapes a hosting platform actually produces, and what they must become.
POSTGRES_CASES = [
    # Heroku-style legacy scheme, still emitted by Railway.
    ("postgres://u:p@host:5432/db", "postgresql+asyncpg://u:p@host:5432/db"),
    # No driver at all: SQLAlchemy would reach for psycopg2.
    ("postgresql://u:p@host:5432/db", "postgresql+asyncpg://u:p@host:5432/db"),
    # An explicitly synchronous driver is equally unusable here.
    ("postgresql+psycopg2://u:p@host:5432/db", "postgresql+asyncpg://u:p@host:5432/db"),
    ("postgresql+psycopg://u:p@host:5432/db", "postgresql+asyncpg://u:p@host:5432/db"),
    ("postgresql+pg8000://u:p@host:5432/db", "postgresql+asyncpg://u:p@host:5432/db"),
]


@pytest.mark.parametrize("raw,expected", POSTGRES_CASES)
def test_postgres_urls_are_rewritten_onto_asyncpg(raw, expected):
    assert normalise_database_url(raw) == expected


def test_an_asyncpg_url_is_left_exactly_as_it_was():
    url = "postgresql+asyncpg://u:p@host:5432/db"
    assert normalise_database_url(url) == url


def test_an_aiosqlite_url_is_left_exactly_as_it_was():
    assert normalise_database_url(LOCAL_SQLITE_URL) == LOCAL_SQLITE_URL


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("sqlite:///./local.db", "sqlite+aiosqlite:///./local.db"),
        ("sqlite:///:memory:", "sqlite+aiosqlite:///:memory:"),
        ("sqlite+aiosqlite:///./x.db", "sqlite+aiosqlite:///./x.db"),
    ],
)
def test_sqlite_development_urls_keep_working(raw, expected):
    assert normalise_database_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_a_missing_url_falls_back_to_local_sqlite(raw):
    assert normalise_database_url(raw) == LOCAL_SQLITE_URL


# --- credentials and query parameters --------------------------------------


def test_the_password_survives_the_rewrite():
    result = normalise_database_url("postgres://user:s3cr3t@host:5432/db")
    assert "s3cr3t" in result
    assert result.startswith("postgresql+asyncpg://")


def test_an_encoded_password_is_not_mangled():
    # A password containing '@' and ':' arrives percent-encoded.
    raw = "postgres://user:pa%40ss%3Aword@host:5432/db"
    result = normalise_database_url(raw)
    assert result == "postgresql+asyncpg://user:pa%40ss%3Aword@host:5432/db"


def test_sslmode_is_translated_to_the_spelling_asyncpg_understands():
    # libpq says "sslmode"; asyncpg only accepts "ssl" and would otherwise
    # raise TypeError about an unexpected keyword at connect time.
    result = normalise_database_url("postgres://u:p@host:5432/db?sslmode=require")
    assert result == "postgresql+asyncpg://u:p@host:5432/db?ssl=require"


def test_an_explicit_ssl_parameter_wins_over_sslmode():
    result = normalise_database_url(
        "postgresql://u:p@host/db?ssl=verify-full&sslmode=require"
    )
    assert "ssl=verify-full" in result
    assert "sslmode" not in result


def test_other_query_parameters_are_preserved():
    result = normalise_database_url(
        "postgres://u:p@host:5432/db?application_name=atreox&connect_timeout=10"
    )
    assert "application_name=atreox" in result
    assert "connect_timeout=10" in result


def test_an_unknown_backend_is_left_alone():
    # Not ours to rewrite - fail loudly later rather than guess.
    url = "mysql+aiomysql://u:p@host/db"
    assert normalise_database_url(url) == url


# --- the engine actually builds --------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [raw for raw, _ in POSTGRES_CASES]
    + [
        "postgres://u:p@host:5432/db?sslmode=require",
        "sqlite:///./x.db",
        LOCAL_SQLITE_URL,
    ],
)
def test_every_variant_produces_a_working_async_engine(raw):
    """The regression itself: this used to raise ModuleNotFoundError: psycopg2."""
    engine = create_async_engine(normalise_database_url(raw))
    # Building the connect arguments is where a bad driver keyword surfaces.
    _, connect_args = engine.dialect.create_connect_args(engine.url)
    assert engine.dialect.is_async
    assert engine.dialect.driver in {"asyncpg", "aiosqlite"}
    # asyncpg.connect() has no sslmode keyword.
    assert "sslmode" not in connect_args


# --- wired through the settings layer --------------------------------------


def test_settings_normalise_the_url_from_the_environment(clean_env):
    clean_env.setenv("BOT_TOKEN", "1:a")
    clean_env.setenv("DATABASE_URL", "postgres://u:p@host:5432/db")
    settings = Settings()
    assert settings.database_url == "postgresql+asyncpg://u:p@host:5432/db"
    assert settings.uses_sqlite is False


def test_settings_keep_sqlite_for_local_development(clean_env):
    clean_env.setenv("BOT_TOKEN", "1:a")
    settings = Settings()
    assert settings.database_url == LOCAL_SQLITE_URL
    assert settings.uses_sqlite is True


def test_settings_accept_a_railway_style_url_with_ssl(clean_env):
    clean_env.setenv("BOT_TOKEN", "1:a")
    clean_env.setenv(
        "DATABASE_URL", "postgresql://postgres:pw@containers.railway.app:6543/railway?sslmode=require"
    )
    settings = Settings()
    assert settings.database_url.startswith("postgresql+asyncpg://")
    assert "ssl=require" in settings.database_url
    assert "sslmode" not in settings.database_url
