"""Configurable sticker discovery categories.

The keys are stable identifiers used in callback data, in the DB
``sticker_sets.category`` column and in the seed fixture.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StickerCategory:
    key: str
    label: str


STICKER_CATEGORIES: tuple[StickerCategory, ...] = (
    StickerCategory(key="reactions", label="😂 Reactions"),
    StickerCategory(key="cute", label="🥰 Cute"),
    StickerCategory(key="anime", label="🎌 Anime"),
)

CATEGORY_KEYS = frozenset(c.key for c in STICKER_CATEGORIES)


def get_category(key: str) -> StickerCategory | None:
    for category in STICKER_CATEGORIES:
        if category.key == key:
            return category
    return None
