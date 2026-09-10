"""Catch-all: any stray message returns the user to a usable state."""

from __future__ import annotations

from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot import texts
from app.bot.keyboards.common import main_menu

router = Router(name="fallback")


@router.message()
async def handle_anything(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(texts.MAIN_MENU, reply_markup=main_menu())
