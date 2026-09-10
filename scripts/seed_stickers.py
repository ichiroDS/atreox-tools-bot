"""Seed the curated sticker catalog from a JSON/YAML fixture.

Usage:
    python -m scripts.seed_stickers scripts/fixtures/stickers.yaml

The load is idempotent: sets are matched on ``telegram_set_name`` and their
samples are replaced, so re-running after editing the fixture is safe.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.db.models import StickerSample, StickerSet  # noqa: E402
from app.db.session import create_engine, create_session_factory  # noqa: E402
from app.services.stickers.catalog import CATEGORY_KEYS  # noqa: E402


def load_fixture(path: Path) -> list[dict[str, Any]]:
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw) if path.suffix.lower() == ".json" else yaml.safe_load(raw)
    sets = data.get("sticker_sets") if isinstance(data, dict) else data
    if not isinstance(sets, list):
        raise ValueError("fixture must contain a 'sticker_sets' list")
    return sets


def validate_fixture(sets: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    for entry in sets:
        name = entry.get("telegram_set_name")
        if not name:
            raise ValueError("every sticker set needs a telegram_set_name")
        if name in seen:
            raise ValueError(f"duplicate sticker set in fixture: {name}")
        seen.add(name)
        category = entry.get("category")
        if category not in CATEGORY_KEYS:
            raise ValueError(
                f"{name}: unknown category {category!r} "
                f"(known: {sorted(CATEGORY_KEYS)})"
            )
        samples = entry.get("samples") or []
        if not samples:
            raise ValueError(f"{name}: at least one sample is required")
        for sample in samples:
            if not sample.get("file_id"):
                raise ValueError(f"{name}: a sample is missing file_id")


async def seed(fixture_path: Path) -> tuple[int, int]:
    entries = load_fixture(fixture_path)
    validate_fixture(entries)

    settings = get_settings()
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)

    set_count = sample_count = 0
    try:
        async with session_factory() as session:
            for entry in entries:
                sticker_set = await session.scalar(
                    select(StickerSet).where(
                        StickerSet.telegram_set_name == entry["telegram_set_name"]
                    )
                )
                if sticker_set is None:
                    sticker_set = StickerSet(
                        telegram_set_name=entry["telegram_set_name"]
                    )
                    session.add(sticker_set)

                sticker_set.title = entry.get("title") or entry["telegram_set_name"]
                sticker_set.category = entry["category"]
                sticker_set.enabled = bool(entry.get("enabled", True))
                # Replace samples wholesale so the fixture is the source of truth.
                sticker_set.samples = [
                    StickerSample(
                        telegram_file_id=sample["file_id"],
                        emoji=sample.get("emoji"),
                        weight=int(sample.get("weight", 1)),
                        enabled=bool(sample.get("enabled", True)),
                    )
                    for sample in entry["samples"]
                ]
                set_count += 1
                sample_count += len(sticker_set.samples)

            await session.commit()
    finally:
        await engine.dispose()

    return set_count, sample_count


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the sticker catalog")
    parser.add_argument(
        "fixture",
        nargs="?",
        default="scripts/fixtures/stickers.yaml",
        type=Path,
        help="path to a JSON or YAML fixture",
    )
    args = parser.parse_args()

    sets, samples = asyncio.run(seed(args.fixture))
    print(f"seeded {sets} sticker sets / {samples} samples from {args.fixture}")


if __name__ == "__main__":
    main()
