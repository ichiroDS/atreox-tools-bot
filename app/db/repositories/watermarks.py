"""Saved watermark presets.

Every read and write is scoped to one Telegram user, so one user's presets can
never be listed, edited or deleted through another's id.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MAX_PRESETS_PER_USER, WatermarkPreset


class PresetLimitReached(Exception):
    """The user already has :data:`MAX_PRESETS_PER_USER` presets."""


class DuplicateName(Exception):
    """The user already has a preset under that name."""


class WatermarkPresetsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for(self, telegram_user_id: int) -> list[WatermarkPreset]:
        result = await self._session.scalars(
            select(WatermarkPreset)
            .where(WatermarkPreset.telegram_user_id == telegram_user_id)
            .order_by(WatermarkPreset.created_at, WatermarkPreset.id)
        )
        return list(result)

    async def count_for(self, telegram_user_id: int) -> int:
        return int(
            await self._session.scalar(
                select(func.count())
                .select_from(WatermarkPreset)
                .where(WatermarkPreset.telegram_user_id == telegram_user_id)
            )
            or 0
        )

    async def get(self, preset_id: int, telegram_user_id: int) -> WatermarkPreset | None:
        """One preset, but only if it belongs to this user."""
        return await self._session.scalar(
            select(WatermarkPreset).where(
                WatermarkPreset.id == preset_id,
                WatermarkPreset.telegram_user_id == telegram_user_id,
            )
        )

    async def create(
        self,
        telegram_user_id: int,
        *,
        name: str,
        text: str,
        position: str,
        style: str,
        size: str,
        opacity: int,
    ) -> WatermarkPreset:
        if await self.count_for(telegram_user_id) >= MAX_PRESETS_PER_USER:
            raise PresetLimitReached(str(MAX_PRESETS_PER_USER))
        if await self._named(telegram_user_id, name) is not None:
            raise DuplicateName(name)

        now = datetime.now(timezone.utc)
        preset = WatermarkPreset(
            telegram_user_id=telegram_user_id,
            name=name,
            text=text,
            position=position,
            style=style,
            size=size,
            opacity=opacity,
            created_at=now,
            updated_at=now,
        )
        self._session.add(preset)
        await self._session.flush()
        return preset

    async def rename(self, preset: WatermarkPreset, name: str) -> WatermarkPreset:
        existing = await self._named(preset.telegram_user_id, name)
        if existing is not None and existing.id != preset.id:
            raise DuplicateName(name)
        preset.name = name
        return await self._touch(preset)

    async def set_text(self, preset: WatermarkPreset, text: str) -> WatermarkPreset:
        preset.text = text
        return await self._touch(preset)

    async def set_look(
        self, preset: WatermarkPreset, *, position: str, style: str, size: str, opacity: int
    ) -> WatermarkPreset:
        preset.position, preset.style = position, style
        preset.size, preset.opacity = size, opacity
        return await self._touch(preset)

    async def delete(self, preset: WatermarkPreset) -> None:
        await self._session.delete(preset)
        await self._session.flush()

    async def _named(self, telegram_user_id: int, name: str) -> WatermarkPreset | None:
        return await self._session.scalar(
            select(WatermarkPreset).where(
                WatermarkPreset.telegram_user_id == telegram_user_id,
                WatermarkPreset.name == name,
            )
        )

    async def _touch(self, preset: WatermarkPreset) -> WatermarkPreset:
        preset.updated_at = datetime.now(timezone.utc)
        await self._session.flush()
        return preset
