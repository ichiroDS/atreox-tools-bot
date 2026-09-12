"""Resolve the curated sticker packs against Telegram and fill the catalog.

Telegram ``file_id`` values are bot-specific, so they cannot be committed to a
fixture: they have to be fetched by the bot that will later send them. This
script calls ``getStickerSet`` for every pack in
``app.services.stickers.packs`` and stores the real ids.

    python -m scripts.sync_stickers

Re-running is safe: packs are matched on ``telegram_set_name`` and their
samples are replaced, so it doubles as a refresh when a pack changes.
A pack Telegram no longer serves is skipped with a warning rather than
aborting the run.

Packs the catalog no longer lists are deleted, so dropping a pack - or a whole
category - from ``packs.py`` actually removes it from what users see instead of
leaving an orphan row behind.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.db.models import StickerSample, StickerSet  # noqa: E402
from app.db.session import create_engine, create_session_factory  # noqa: E402
from app.main import create_bot  # noqa: E402
from app.services.stickers.packs import CURATED_PACKS, CuratedPack  # noqa: E402

# Enough variety that "More" keeps feeling fresh, without storing whole packs.
SAMPLES_PER_PACK = 8


async def resolve_pack(bot, pack: CuratedPack) -> tuple[str, list[tuple[str, str]]] | None:
    """Return ``(title, [(file_id, emoji), ...])`` or None if unavailable."""
    try:
        sticker_set = await bot.get_sticker_set(pack.telegram_set_name)
    except Exception as exc:  # noqa: BLE001 - a pack may be gone or renamed
        print(f"  skip {pack.telegram_set_name}: {exc}")
        return None

    # Static stickers only: an animated .tgs sent as a plain sticker still
    # works, but the picker reads better when every result renders the same.
    stickers = [s for s in sticker_set.stickers if not s.is_animated and not s.is_video]
    stickers = stickers or list(sticker_set.stickers)
    samples = [(s.file_id, s.emoji or "") for s in stickers[:SAMPLES_PER_PACK]]
    if not samples:
        print(f"  skip {pack.telegram_set_name}: pack is empty")
        return None
    return pack.title or sticker_set.title, samples


async def sync(dry_run: bool = False) -> tuple[int, int, int]:
    settings = get_settings()
    bot = create_bot(settings)
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)

    pack_count = sample_count = 0
    try:
        async with session_factory() as session:
            for pack in CURATED_PACKS:
                resolved = await resolve_pack(bot, pack)
                if resolved is None:
                    continue
                title, samples = resolved
                print(f"  {pack.telegram_set_name:18s} {title[:28]:30s} {len(samples)} samples")
                pack_count += 1
                sample_count += len(samples)
                if dry_run:
                    continue

                sticker_set = await session.scalar(
                    select(StickerSet).where(
                        StickerSet.telegram_set_name == pack.telegram_set_name
                    )
                )
                if sticker_set is None:
                    sticker_set = StickerSet(
                        telegram_set_name=pack.telegram_set_name
                    )
                    session.add(sticker_set)

                sticker_set.title = title
                sticker_set.category = pack.category
                sticker_set.enabled = True
                # Replace wholesale so the curated list stays the source of truth.
                sticker_set.samples = [
                    StickerSample(telegram_file_id=file_id, emoji=emoji or None)
                    for file_id, emoji in samples
                ]

            removed = await prune(session, dry_run=dry_run)
            if not dry_run:
                await session.commit()
    finally:
        await bot.session.close()
        await engine.dispose()

    return pack_count, sample_count, removed


async def prune(session, *, dry_run: bool = False) -> int:
    """Delete catalog rows for packs that are no longer curated."""
    curated = {pack.telegram_set_name for pack in CURATED_PACKS}
    stale = [
        sticker_set
        for sticker_set in (await session.scalars(select(StickerSet))).all()
        if sticker_set.telegram_set_name not in curated
    ]
    for sticker_set in stale:
        print(f"  remove {sticker_set.telegram_set_name} ({sticker_set.category})")
        if not dry_run:
            # Samples go with it: the relationship cascades.
            await session.delete(sticker_set)
    return len(stale)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync the sticker catalog")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve packs without writing to the database",
    )
    args = parser.parse_args()

    packs, samples, removed = asyncio.run(sync(dry_run=args.dry_run))
    verb = "would sync" if args.dry_run else "synced"
    print(f"{verb} {packs} packs / {samples} samples, {removed} no longer curated")


if __name__ == "__main__":
    main()
