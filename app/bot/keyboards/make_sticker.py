"""Keyboards for Make Sticker."""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot import texts
from app.bot.callbacks import MakeStickerCallback, MenuCallback

STYLE_LABELS = {
    "clean": texts.BTN_STICKER_CLEAN,
    "white_outline": texts.BTN_STICKER_WHITE_OUTLINE,
    "black_outline": texts.BTN_STICKER_BLACK_OUTLINE,
}


def style_choices() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for value, label in STYLE_LABELS.items():
        builder.button(
            text=label, callback_data=MakeStickerCallback(action="style", value=value)
        )
    builder.button(text=texts.BTN_CANCEL, callback_data=MakeStickerCallback(action="cancel"))
    builder.adjust(1)
    return builder.as_markup()


def sticker_done() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=texts.BTN_STICKER_MAKE_AGAIN, callback_data=MenuCallback(action="make_sticker")
    )
    builder.button(text=texts.BTN_MAIN_MENU_HOME, callback_data=MenuCallback(action="main"))
    builder.adjust(1)
    return builder.as_markup()
