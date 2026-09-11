from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.models import Base  # noqa: E402
from app.db.session import create_engine, create_session_factory  # noqa: E402

_ENV_KEYS = (
    "BOT_TOKEN",
    "BOT_API_BASE_URL",
    "BOT_API_FILES_URL",
    "BOT_API_LOCAL_DIR",
    "DATABASE_URL",
    "LOG_LEVEL",
    "MAX_INPUT_FILE_SIZE_MB",
    "MAX_OUTPUT_FILE_SIZE_MB",
    "MAX_CONCURRENT_MEDIA_JOBS",
    "TEMP_DISK_BUDGET_MB",
    "PROCESS_TIMEOUT_SECONDS",
    "TEMP_ROOT",
    "ADMIN_USER_IDS",
    "STICKERS_PER_BATCH",
    "VIDEO_NOTE_SIZE",
    "VIDEO_NOTE_MAX_DURATION",
)


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """Isolate Settings from the developer's real .env file."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    return monkeypatch


@pytest_asyncio.fixture
async def session():
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with factory() as db_session:
        yield db_session
    await engine.dispose()


class FakeSample(SimpleNamespace):
    """Duck-typed stand-in for a StickerSample row."""


class FakeSet(SimpleNamespace):
    """Duck-typed stand-in for a StickerSet row."""


def make_sample(file_id: str, *, emoji: str = "🙂", weight: int = 1, enabled: bool = True):
    return FakeSample(telegram_file_id=file_id, emoji=emoji, weight=weight, enabled=enabled)


def make_set(name: str, samples, *, title: str | None = None):
    return FakeSet(telegram_set_name=name, title=title or name, samples=list(samples))
