"""Keyboards for the Media Optimizer flow."""

from __future__ import annotations

from typing import Iterable, Mapping

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot import texts
from app.bot.callbacks import MenuCallback, OptimizeCallback

_PRESET_LABELS = {
    "small": texts.BTN_OPTIMIZE_SMALL,
    "balanced": texts.BTN_OPTIMIZE_BALANCED,
    "high": texts.BTN_OPTIMIZE_HIGH,
}


def preset_choices(
    presets: Iterable[str], estimates: Mapping[str, int | None] | None = None
) -> InlineKeyboardMarkup:
    """One button per offered preset, with its size estimate when there is one."""
    estimates = estimates or {}
    builder = InlineKeyboardBuilder()
    for preset in presets:
        builder.button(
            text=texts.preset_button(_PRESET_LABELS[preset], estimates.get(preset)),
            callback_data=OptimizeCallback(action="preset", value=preset),
        )
    builder.button(text=texts.BTN_CANCEL, callback_data=OptimizeCallback(action="cancel"))
    builder.adjust(1)
    return builder.as_markup()


def optimize_done() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    # Reuses the menu entry point, as the other tools' "again" buttons do.
    builder.button(text=texts.BTN_OPTIMIZE_AGAIN, callback_data=MenuCallback(action="optimize"))
    builder.button(text=texts.BTN_MAIN_MENU_HOME, callback_data=MenuCallback(action="main"))
    builder.adjust(1)
    return builder.as_markup()
