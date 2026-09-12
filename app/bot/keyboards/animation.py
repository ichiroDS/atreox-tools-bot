"""Keyboards for the GIF / MP4 converter."""

from __future__ import annotations

from typing import Iterable

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot import texts
from app.bot.callbacks import AnimationCallback, MenuCallback

# One label per conversion, keyed by the value the service uses.
_CONVERSION_LABELS = {
    "to_gif": texts.BTN_ANIMATION_TO_GIF,
    "to_mp4": texts.BTN_ANIMATION_TO_MP4,
    "optimize_gif": texts.BTN_ANIMATION_OPTIMIZE_GIF,
}
_CLIP_LABELS = {
    "first": texts.BTN_ANIMATION_CLIP_FIRST,
    "middle": texts.BTN_ANIMATION_CLIP_MIDDLE,
    "custom": texts.BTN_ANIMATION_CLIP_CUSTOM,
}


def _cancel(builder: InlineKeyboardBuilder) -> None:
    builder.button(text=texts.BTN_CANCEL, callback_data=AnimationCallback(action="cancel"))


def conversion_choices(conversions: Iterable[str]) -> InlineKeyboardMarkup:
    """What this particular file can become - a video and a GIF differ."""
    builder = InlineKeyboardBuilder()
    for conversion in conversions:
        builder.button(
            text=_CONVERSION_LABELS[conversion],
            callback_data=AnimationCallback(action="convert", value=conversion),
        )
    _cancel(builder)
    builder.adjust(1)
    return builder.as_markup()


def clip_choices() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for value, label in _CLIP_LABELS.items():
        builder.button(text=label, callback_data=AnimationCallback(action="clip", value=value))
    _cancel(builder)
    builder.adjust(2, 1, 1)
    return builder.as_markup()


def animation_done() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=texts.BTN_ANIMATION_AGAIN, callback_data=MenuCallback(action="animation"))
    builder.button(text=texts.BTN_MAIN_MENU_HOME, callback_data=MenuCallback(action="main"))
    builder.adjust(1)
    return builder.as_markup()
