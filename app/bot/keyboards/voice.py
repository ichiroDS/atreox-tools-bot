"""Keyboards for the Audio / Video -> Voice Note flow."""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot import texts
from app.bot.callbacks import MenuCallback, VoiceCallback


def voice_done() -> InlineKeyboardMarkup:
    """Shown under the success message once the voice note has been sent."""
    builder = InlineKeyboardBuilder()
    # "Make another" reuses the menu entry point, as the circle flow does.
    builder.button(text=texts.BTN_VOICE_AGAIN, callback_data=MenuCallback(action="voice"))
    builder.button(
        text=texts.BTN_VOICE_FORWARD_HELP,
        callback_data=VoiceCallback(action="forward_help"),
    )
    builder.button(text=texts.BTN_MAIN_MENU_HOME, callback_data=MenuCallback(action="main"))
    builder.adjust(1)
    return builder.as_markup()


def voice_forward_help() -> InlineKeyboardMarkup:
    """Platform switcher under the "hide the bot name" guide; Back returns to
    the voice result rather than the circle one."""
    builder = InlineKeyboardBuilder()
    builder.button(text=texts.BTN_GUIDE_IOS, callback_data=VoiceCallback(action="guide_ios"))
    builder.button(
        text=texts.BTN_GUIDE_ANDROID,
        callback_data=VoiceCallback(action="guide_android"),
    )
    builder.button(text=texts.BTN_BACK, callback_data=VoiceCallback(action="forward_back"))
    builder.adjust(2, 1)
    return builder.as_markup()
