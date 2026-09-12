"""Keyboards for Extract Frame."""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot import texts
from app.bot.callbacks import FrameCallback, MenuCallback

# Keyed by the value the service uses, so copy and code cannot drift.
POSITION_LABELS = {
    "first": texts.BTN_FRAME_FIRST,
    "quarter": texts.BTN_FRAME_QUARTER,
    "middle": texts.BTN_FRAME_MIDDLE,
    "three_quarter": texts.BTN_FRAME_THREE_QUARTER,
    "custom": texts.BTN_FRAME_CUSTOM,
}
FORMAT_LABELS = {"jpeg": texts.BTN_FRAME_JPG, "png": texts.BTN_FRAME_PNG}


def _cancel(builder: InlineKeyboardBuilder) -> None:
    builder.button(text=texts.BTN_CANCEL, callback_data=FrameCallback(action="cancel"))


def position_choices() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for value, label in POSITION_LABELS.items():
        builder.button(text=label, callback_data=FrameCallback(action="pos", value=value))
    _cancel(builder)
    builder.adjust(2, 2, 1, 1)
    return builder.as_markup()


def format_choices() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for value, label in FORMAT_LABELS.items():
        builder.button(text=label, callback_data=FrameCallback(action="format", value=value))
    _cancel(builder)
    builder.adjust(2, 1)
    return builder.as_markup()


def frame_done() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=texts.BTN_FRAME_AGAIN, callback_data=MenuCallback(action="frame"))
    builder.button(text=texts.BTN_MAIN_MENU_HOME, callback_data=MenuCallback(action="main"))
    builder.adjust(1)
    return builder.as_markup()
