"""Keyboards for the Sticker Finder, generated from the category catalog."""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot import texts
from app.bot.callbacks import MenuCallback, StickerCallback
from app.services.stickers.catalog import STICKER_CATEGORIES


def category_choices() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for category in STICKER_CATEGORIES:
        builder.button(
            text=category.label,
            callback_data=StickerCallback(action="category", category=category.key),
        )
    builder.button(text=texts.BTN_MAIN_MENU, callback_data=MenuCallback(action="main"))
    builder.adjust(1)
    return builder.as_markup()


def result_controls(category_key: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=texts.BTN_MORE,
        callback_data=StickerCallback(action="more", category=category_key),
    )
    builder.button(
        text=texts.BTN_CATEGORIES, callback_data=StickerCallback(action="categories")
    )
    builder.button(text=texts.BTN_MAIN_MENU, callback_data=MenuCallback(action="main"))
    builder.adjust(2, 1)
    return builder.as_markup()
