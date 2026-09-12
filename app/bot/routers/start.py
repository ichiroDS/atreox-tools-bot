"""/start, main menu, help, privacy and the AtreoxAI call to action."""

from __future__ import annotations

import logging
import re

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.callbacks import MenuCallback
from app.bot.keyboards.common import (
    cta_open_keyboard,
    help_menu,
    main_menu,
    privacy_menu,
)
from app.db.models import Feature, User
from app.db.repositories import EventsRepository

logger = logging.getLogger(__name__)

router = Router(name="start")

_SOURCE_RE = re.compile(r"[^A-Za-z0-9_-]")
# Everyone who arrives without a usable deep link is attributed here.
DEFAULT_SOURCE = "direct"


def sanitise_source(raw: str | None) -> str:
    """Deep-link payloads are user controlled - keep them boring and short.

    Anything empty, or made entirely of characters we refuse, collapses to
    ``direct`` rather than being stored as-is or left blank.
    """
    if not raw:
        return DEFAULT_SOURCE
    cleaned = _SOURCE_RE.sub("", raw)[:64]
    return cleaned or DEFAULT_SOURCE


# CommandStart() matches with and without a deep-link payload; the payload
# arrives as ``command.args``.
@router.message(CommandStart())
async def handle_start(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    user: User | None = None,
    is_new_user: bool = False,
) -> None:
    await state.clear()

    # First touch wins: a user who later arrives through a different link keeps
    # the source that originally brought them in.
    if user is not None and not user.source:
        user.source = sanitise_source(command.args)
        logger.info(
            "acquisition source=%s telegram_user_id=%s new=%s",
            user.source,
            user.telegram_user_id,
            is_new_user,
        )

    await message.answer(texts.START, reply_markup=main_menu())


@router.message(Command("help"))
async def handle_help_command(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(texts.HELP, reply_markup=help_menu())


@router.message(Command("privacy"))
async def handle_privacy_command(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(texts.PRIVACY, reply_markup=privacy_menu())


@router.message(Command("cancel"))
async def handle_cancel_command(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(texts.CANCELLED, reply_markup=main_menu())


@router.message(Command("restart"))
async def handle_restart_command(message: Message, state: FSMContext) -> None:
    """Throw away everything the bot remembers about this conversation.

    Half-finished wizards, a collected batch, an uploaded logo waiting for its
    position - all of it lives in this chat's session, and this drops the lot
    and starts again from the menu. Saved presets are deliberately untouched:
    they are the one thing the user asked us to keep.

    It does not restart the process. One user's stuck wizard is no reason to
    interrupt everybody else's uploads, and a command anyone can send must
    never be able to take the bot down.
    """
    await state.clear()
    logger.info(
        "session reset telegram_user_id=%s",
        message.from_user.id if message.from_user else "-",
    )
    await message.answer(texts.RESTARTED, reply_markup=main_menu())


@router.callback_query(MenuCallback.filter(F.action == "main"))
async def handle_main_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _replace(callback, texts.MAIN_MENU, main_menu())


@router.callback_query(MenuCallback.filter(F.action == "help"))
async def handle_help(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession | None = None,
) -> None:
    await state.clear()
    await _record(session, callback, Feature.HELP)
    await _replace(callback, texts.HELP, help_menu())


@router.callback_query(MenuCallback.filter(F.action == "privacy"))
async def handle_privacy(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession | None = None,
) -> None:
    await state.clear()
    await _record(session, callback, Feature.PRIVACY)
    await _replace(callback, texts.PRIVACY, privacy_menu())


@router.callback_query(MenuCallback.filter(F.action == "cta"))
async def handle_cta(
    callback: CallbackQuery,
    session: AsyncSession | None = None,
) -> None:
    """Count the tap, then hand over the link.

    Telegram reports nothing about plain URL buttons, so the promo uses a
    callback and this handler supplies the real link.
    """
    await _record(session, callback, Feature.CTA_CLICK)
    await _replace(callback, texts.CTA_OPEN, cta_open_keyboard())


async def _record(
    session: AsyncSession | None, callback: CallbackQuery, feature: Feature
) -> None:
    if session is not None and callback.from_user is not None:
        await EventsRepository(session).record(callback.from_user.id, feature)


async def _replace(callback: CallbackQuery, text: str, markup) -> None:
    """Edit the menu in place, falling back to a new message when Telegram
    refuses (e.g. the previous message was a sticker)."""
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(text, reply_markup=markup)
        except Exception:  # noqa: BLE001 - message may not be editable
            await callback.message.answer(text, reply_markup=markup)
    await callback.answer()
