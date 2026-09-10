"""Admin-only helpers: catalog building and internal stats.

Both are gated by ``IsAdmin``. For everyone else the filter simply does not
match, so the message falls through to the normal fallback and no admin data
is ever revealed - not even the fact that the command exists.
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, Filter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.keyboards.common import back_to_menu
from app.config import Settings
from app.db.repositories import StatsRepository

logger = logging.getLogger(__name__)

router = Router(name="admin")


class IsAdmin(Filter):
    async def __call__(self, event: Message, **data: Any) -> bool:
        settings: Settings | None = data.get("settings")
        user = getattr(event, "from_user", None)
        if settings is None or user is None:
            return False
        return user.id in settings.admin_ids


@router.message(Command("stats"), IsAdmin())
async def show_stats(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    await state.clear()
    stats = await StatsRepository(session).collect()
    logger.info(
        "stats requested by telegram_user_id=%s",
        message.from_user.id if message.from_user else 0,
    )
    await message.answer(texts.stats_report(stats), reply_markup=back_to_menu())


@router.message(F.sticker, IsAdmin())
async def show_sticker_ids(message: Message) -> None:
    sticker = message.sticker
    await message.answer(
        "<b>Sticker debug</b>\n"
        f"set: <code>{sticker.set_name or '-'}</code>\n"
        f"emoji: {sticker.emoji or '-'}\n"
        f"file_id:\n<code>{sticker.file_id}</code>"
    )
