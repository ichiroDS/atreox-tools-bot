"""Sticker Finder: discovery across many packs, one sticker per pack."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.callbacks import MenuCallback, StickerCallback
from app.bot.keyboards.stickers import category_choices, result_controls
from app.config import Settings
from app.db.models import Feature
from app.db.repositories import EventsRepository, StickersRepository
from app.services.ratelimit import RateLimiter
from app.services.stickers import StickerFinderService, get_category

logger = logging.getLogger(__name__)

router = Router(name="stickers")

_LAST_BATCH_KEY = "sticker_last_sets"


@router.callback_query(MenuCallback.filter(F.action == "stickers"))
async def open_stickers(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession | None = None,
) -> None:
    await state.clear()
    if session is not None and callback.from_user is not None:
        await EventsRepository(session).record(callback.from_user.id, Feature.STICKERS)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            texts.STICKERS_PROMPT, reply_markup=category_choices()
        )
    await callback.answer()


@router.callback_query(StickerCallback.filter(F.action == "categories"))
async def show_categories(callback: CallbackQuery) -> None:
    if isinstance(callback.message, Message):
        await callback.message.answer(
            texts.STICKERS_PROMPT, reply_markup=category_choices()
        )
    await callback.answer()


@router.callback_query(StickerCallback.filter(F.action.in_({"category", "more"})))
async def send_batch(
    callback: CallbackQuery,
    callback_data: StickerCallback,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    session: AsyncSession,
    limiter: RateLimiter | None = None,
) -> None:
    await callback.answer()
    category = get_category(callback_data.category)
    message = callback.message
    if category is None or not isinstance(message, Message):
        return

    user_id = callback.from_user.id if callback.from_user else 0
    if limiter is not None and not limiter.allow("stickers", user_id):
        # The category keyboard stays available, so retrying is one tap.
        await message.answer(
            texts.RATE_LIMITED, reply_markup=result_controls(category.key)
        )
        return

    data = await state.get_data()
    exclude = data.get(_LAST_BATCH_KEY, []) if callback_data.action == "more" else []

    service = StickerFinderService(StickersRepository(session))
    picks = await service.find(
        category.key, count=settings.stickers_per_batch, exclude_set_names=exclude
    )

    if not picks:
        await message.answer(texts.STICKERS_EMPTY, reply_markup=category_choices())
        return

    delivered: list[str] = []
    for pick in picks:
        try:
            await bot.send_sticker(message.chat.id, pick.file_id)
        except Exception:  # noqa: BLE001 - a stale file_id must not kill the batch
            logger.warning("failed to send sticker from set=%s", pick.set_name)
            continue
        delivered.append(pick.set_name)

    if not delivered:
        await message.answer(texts.STICKERS_EMPTY, reply_markup=category_choices())
        return

    await state.update_data(**{_LAST_BATCH_KEY: delivered})
    await message.answer(
        texts.STICKERS_RESULT_CONTROLS, reply_markup=result_controls(category.key)
    )
