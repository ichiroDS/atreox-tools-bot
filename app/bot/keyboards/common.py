"""Menu keyboards built from configuration, never hardcoded per handler."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot import texts
from app.bot.callbacks import MenuCallback


def main_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=texts.BTN_CIRCLE, callback_data=MenuCallback(action="circle"))
    builder.button(text=texts.BTN_VOICE, callback_data=MenuCallback(action="voice"))
    builder.button(text=texts.BTN_OPTIMIZE, callback_data=MenuCallback(action="optimize"))
    builder.button(text=texts.BTN_WATERMARK, callback_data=MenuCallback(action="watermark"))
    builder.button(text=texts.BTN_BATCH, callback_data=MenuCallback(action="batch"))
    builder.button(text=texts.BTN_METADATA, callback_data=MenuCallback(action="metadata"))
    builder.button(text=texts.BTN_STICKERS, callback_data=MenuCallback(action="stickers"))
    # A direct link: one tap to the site, as intended for the menu entry.
    builder.button(text=texts.BTN_GROW, url=texts.ATREOX_URL)
    builder.button(text=texts.BTN_HELP, callback_data=MenuCallback(action="help"))
    builder.adjust(1)
    return builder.as_markup()


def back_to_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.BTN_MAIN_MENU,
                    callback_data=MenuCallback(action="main").pack(),
                )
            ]
        ]
    )


def help_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=texts.BTN_PRIVACY, callback_data=MenuCallback(action="privacy"))
    builder.button(text=texts.BTN_ATREOX, url=texts.ATREOX_URL)
    builder.button(text=texts.BTN_MAIN_MENU_HOME, callback_data=MenuCallback(action="main"))
    builder.adjust(1)
    return builder.as_markup()


def privacy_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=texts.BTN_BACK, callback_data=MenuCallback(action="help"))
    builder.button(text=texts.BTN_MAIN_MENU_HOME, callback_data=MenuCallback(action="main"))
    builder.adjust(1)
    return builder.as_markup()


def cta_keyboard() -> InlineKeyboardMarkup:
    """The promo button.

    A callback rather than a plain link so the tap can be counted; the handler
    then hands over the real URL.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text=texts.BTN_CTA, callback_data=MenuCallback(action="cta"))
    return builder.as_markup()


def cta_open_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=texts.BTN_CTA_OPEN, url=texts.ATREOX_URL)
    builder.button(text=texts.BTN_MAIN_MENU_HOME, callback_data=MenuCallback(action="main"))
    builder.adjust(1)
    return builder.as_markup()
