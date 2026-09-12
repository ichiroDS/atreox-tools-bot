"""Sticker discovery.

Atreox Tools is not a pack downloader: we hand the user single stickers, each
one from a *different* pack, so tapping one opens its source pack in Telegram
and they can add it themselves.

Which sticker stands for a pack is *fixed* (see ``packs.sample_position``), not
drawn at random: a pack should look the same every time it comes up, so a
category becomes recognisable. What still varies between taps is which packs a
batch draws from - that is what makes "More" show something new.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import Callable, Iterable, Protocol, Sequence

from app.services.stickers.packs import sample_position_for

logger = logging.getLogger(__name__)


class _Sample(Protocol):
    telegram_file_id: str
    emoji: str | None
    weight: int
    enabled: bool


class _StickerSet(Protocol):
    telegram_set_name: str
    title: str
    samples: Sequence[_Sample]


@dataclass(frozen=True)
class StickerPick:
    """One sticker chosen to represent its pack."""

    set_name: str
    set_title: str
    file_id: str
    emoji: str | None = None


def _pick_sample(samples: Sequence[_Sample], position: int) -> _Sample | None:
    """The pack's chosen sticker, or its last one when the pack is shorter.

    ``position`` is 1-based. Samples are stored in pack order, so this is the
    same sticker on every call - the point of fixing it.
    """
    usable = [s for s in samples if s.enabled and s.telegram_file_id]
    if not usable:
        return None
    return usable[min(max(1, position), len(usable)) - 1]


def select_distinct_stickers(
    sticker_sets: Iterable[_StickerSet],
    count: int,
    *,
    rng: random.Random | None = None,
    exclude_set_names: Iterable[str] = (),
    position_for: Callable[[str], int] = sample_position_for,
) -> list[StickerPick]:
    """Pick at most ``count`` stickers, never two from the same pack.

    ``exclude_set_names`` lets "More" avoid repeating the previous batch; if
    honouring it would leave us with nothing, we fall back to the full catalog
    rather than returning an empty result.
    """
    rng = rng or random.Random()
    excluded = set(exclude_set_names)

    candidates = [s for s in sticker_sets if s.samples]
    preferred = [s for s in candidates if s.telegram_set_name not in excluded]
    pool = preferred or candidates
    if not pool:
        return []

    shuffled = list(pool)
    rng.shuffle(shuffled)

    picks: list[StickerPick] = []
    seen: set[str] = set()
    for sticker_set in shuffled:
        if len(picks) >= count:
            break
        if sticker_set.telegram_set_name in seen:
            continue
        sample = _pick_sample(
            sticker_set.samples, position_for(sticker_set.telegram_set_name)
        )
        if sample is None:
            continue
        seen.add(sticker_set.telegram_set_name)
        picks.append(
            StickerPick(
                set_name=sticker_set.telegram_set_name,
                set_title=sticker_set.title,
                file_id=sample.telegram_file_id,
                emoji=sample.emoji,
            )
        )
    return picks


class StickerCatalogSource(Protocol):
    async def enabled_sets_for_category(self, category: str) -> list: ...


class StickerFinderService:
    """Thin async wrapper around the pure selection logic."""

    def __init__(
        self, repository: StickerCatalogSource, *, rng: random.Random | None = None
    ) -> None:
        self._repository = repository
        self._rng = rng or random.Random()

    async def find(
        self,
        category: str,
        *,
        count: int,
        exclude_set_names: Iterable[str] = (),
    ) -> list[StickerPick]:
        sticker_sets = await self._repository.enabled_sets_for_category(category)
        picks = select_distinct_stickers(
            sticker_sets,
            count,
            rng=self._rng,
            exclude_set_names=exclude_set_names,
        )
        logger.info(
            "sticker batch category=%s sets=%d picked=%d",
            category,
            len(sticker_sets),
            len(picks),
        )
        return picks
