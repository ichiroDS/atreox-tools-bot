"""Read access to the curated sticker catalog."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import StickerSet


class StickersRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enabled_sets_for_category(self, category: str) -> list[StickerSet]:
        """All enabled packs in a category, samples eagerly loaded."""
        result = await self._session.scalars(
            select(StickerSet)
            .where(StickerSet.category == category, StickerSet.enabled.is_(True))
            .order_by(StickerSet.id)
        )
        return list(result)

    async def count_enabled_sets(self, category: str) -> int:
        sets = await self.enabled_sets_for_category(category)
        return len(sets)
