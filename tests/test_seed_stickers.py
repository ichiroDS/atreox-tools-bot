from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db.models import Base, StickerSet
from app.db.session import create_engine, create_session_factory
from scripts.seed_stickers import load_fixture, seed, validate_fixture

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED_FIXTURE = REPO_ROOT / "scripts" / "fixtures" / "stickers.yaml"


def entry(**overrides):
    base = {
        "telegram_set_name": "PackA",
        "title": "Pack A",
        "category": "cute",
        "samples": [{"file_id": "F1", "emoji": "🥰"}],
    }
    base.update(overrides)
    return base


def test_shipped_fixture_loads_and_validates():
    entries = load_fixture(SHIPPED_FIXTURE)
    validate_fixture(entries)
    assert len(entries) >= 6
    assert len({e["category"] for e in entries}) >= 6


def test_json_fixtures_are_supported(tmp_path):
    path = tmp_path / "stickers.json"
    path.write_text(json.dumps({"sticker_sets": [entry()]}, ensure_ascii=False), encoding="utf-8")
    assert load_fixture(path) == [entry()]


def test_rejects_unknown_category():
    with pytest.raises(ValueError, match="unknown category"):
        validate_fixture([entry(category="memes")])


def test_rejects_duplicate_set_names():
    with pytest.raises(ValueError, match="duplicate"):
        validate_fixture([entry(), entry()])


def test_rejects_a_set_without_samples():
    with pytest.raises(ValueError, match="at least one sample"):
        validate_fixture([entry(samples=[])])


def test_rejects_a_sample_without_a_file_id():
    with pytest.raises(ValueError, match="missing file_id"):
        validate_fixture([entry(samples=[{"emoji": "🥰"}])])


def test_rejects_a_set_without_a_name():
    with pytest.raises(ValueError, match="telegram_set_name"):
        validate_fixture([entry(telegram_set_name=None)])


@pytest.fixture
def sqlite_settings(monkeypatch, tmp_path):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'seed.db').as_posix()}"
    monkeypatch.setenv("BOT_TOKEN", "1:a")
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    yield url
    get_settings.cache_clear()


async def create_schema(url: str) -> None:
    engine = create_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()


async def load_sets(url: str) -> list[StickerSet]:
    engine = create_engine(url)
    factory = create_session_factory(engine)
    async with factory() as session:
        result = await session.scalars(select(StickerSet).order_by(StickerSet.id))
        sets = list(result)
        for sticker_set in sets:
            _ = sticker_set.samples  # eager-loaded via selectin
    await engine.dispose()
    return sets


async def test_seeding_is_idempotent(sqlite_settings, tmp_path):
    await create_schema(sqlite_settings)
    fixture = tmp_path / "stickers.yaml"
    fixture.write_text(
        json.dumps(
            {
                "sticker_sets": [
                    entry(),
                    entry(telegram_set_name="PackB", category="savage"),
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    # ``load_fixture`` reads .json as JSON and everything else as YAML - JSON is
    # valid YAML, so this file works either way.

    assert await seed(fixture) == (2, 2)
    assert [s.telegram_set_name for s in await load_sets(sqlite_settings)] == [
        "PackA",
        "PackB",
    ]

    # Re-running after an edit updates in place instead of duplicating.
    fixture.write_text(
        json.dumps(
            {
                "sticker_sets": [
                    entry(title="Renamed", samples=[{"file_id": "F9"}]),
                    entry(telegram_set_name="PackB", category="savage"),
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert await seed(fixture) == (2, 2)

    sets = await load_sets(sqlite_settings)
    assert len(sets) == 2
    pack_a = next(s for s in sets if s.telegram_set_name == "PackA")
    assert pack_a.title == "Renamed"
    assert [s.telegram_file_id for s in pack_a.samples] == ["F9"]


async def test_seeding_refuses_an_invalid_fixture(sqlite_settings, tmp_path):
    await create_schema(sqlite_settings)
    fixture = tmp_path / "bad.json"
    fixture.write_text(
        json.dumps({"sticker_sets": [entry(category="nope")]}, ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        await seed(fixture)
    assert await load_sets(sqlite_settings) == []
