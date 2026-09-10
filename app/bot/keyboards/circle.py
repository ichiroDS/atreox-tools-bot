"""Keyboards for the Video -> Circle flow."""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot import texts
from app.bot.callbacks import CircleCallback, MenuCallback


def circle_done() -> InlineKeyboardMarkup:
    """Shown under the success message once the video note has been sent."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=texts.BTN_CIRCLE_FORWARD_HELP,
        callback_data=CircleCallback(action="forward_help"),
    )
    # Reuses the menu entry point, so "another circle" and the main menu button
    # both land on the handlers that already own those transitions.
    builder.button(text=texts.BTN_CIRCLE_AGAIN, callback_data=MenuCallback(action="circle"))
    builder.button(text=texts.BTN_MAIN_MENU_HOME, callback_data=MenuCallback(action="main"))
    builder.adjust(1)
    return builder.as_markup()


def forward_help() -> InlineKeyboardMarkup:
    """Platform switcher under the "hide the bot name" guide.

    iOS and Android share the copy for now but keep separate callbacks so a
    platform-specific guide can be added without touching the keyboard.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text=texts.BTN_GUIDE_IOS, callback_data=CircleCallback(action="guide_ios"))
    builder.button(
        text=texts.BTN_GUIDE_ANDROID,
        callback_data=CircleCallback(action="guide_android"),
    )
    builder.button(text=texts.BTN_BACK, callback_data=CircleCallback(action="forward_back"))
    builder.adjust(2, 1)
    return builder.as_markup()
