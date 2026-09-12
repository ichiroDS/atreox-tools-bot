"""Keyboards for Batch Mode."""

from __future__ import annotations

from typing import Iterable

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot import texts
from app.bot.callbacks import BatchCallback, MenuCallback


def _cancel(builder: InlineKeyboardBuilder) -> None:
    builder.button(text=texts.BTN_CANCEL, callback_data=BatchCallback(action="cancel"))


def collecting() -> InlineKeyboardMarkup:
    """Shown while files arrive, on the one message that counts them."""
    builder = InlineKeyboardBuilder()
    builder.button(text=texts.BTN_BATCH_DONE, callback_data=BatchCallback(action="done"))
    _cancel(builder)
    builder.adjust(1)
    return builder.as_markup()


def tool_choices() -> InlineKeyboardMarkup:
    """The three tools a batch supports - deliberately not every tool."""
    builder = InlineKeyboardBuilder()
    for label, value in (
        (texts.BTN_BATCH_CLEAN, "clean"),
        (texts.BTN_BATCH_WATERMARK, "watermark"),
        (texts.BTN_BATCH_OPTIMIZE, "optimize"),
    ):
        builder.button(text=label, callback_data=BatchCallback(action="tool", value=value))
    _cancel(builder)
    builder.adjust(1)
    return builder.as_markup()


def watermark_choices(presets) -> InlineKeyboardMarkup:
    """Saved watermarks, or configure one for this batch."""
    builder = InlineKeyboardBuilder()
    for preset in presets:
        builder.button(
            text=f"💾 {preset.name}",
            callback_data=BatchCallback(action="preset", value=str(preset.id)),
        )
    builder.button(
        text=texts.BTN_BATCH_NEW_WATERMARK, callback_data=BatchCallback(action="new")
    )
    _cancel(builder)
    builder.adjust(1)
    return builder.as_markup()


def optimizer_choices() -> InlineKeyboardMarkup:
    """One preset for the whole batch."""
    builder = InlineKeyboardBuilder()
    for label, value in (
        (texts.BTN_OPTIMIZE_SMALL, "small"),
        (texts.BTN_OPTIMIZE_BALANCED, "balanced"),
        (texts.BTN_OPTIMIZE_HIGH, "high"),
    ):
        builder.button(text=label, callback_data=BatchCallback(action="preset", value=value))
    _cancel(builder)
    builder.adjust(1)
    return builder.as_markup()


def _look_choices(action: str, labels: Iterable[tuple[str, str]], *sizes: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for value, label in labels:
        builder.button(text=label, callback_data=BatchCallback(action=action, value=value))
    _cancel(builder)
    builder.adjust(*sizes)
    return builder.as_markup()


def position_choices() -> InlineKeyboardMarkup:
    return _look_choices("pos", texts.WATERMARK_POSITION_LABELS.items(), 2, 2, 1, 1)


def style_choices() -> InlineKeyboardMarkup:
    return _look_choices("style", texts.WATERMARK_STYLE_LABELS.items(), 1)


def size_choices() -> InlineKeyboardMarkup:
    return _look_choices("size", texts.WATERMARK_SIZE_LABELS.items(), 3, 1)


def opacity_choices(opacities: Iterable[int]) -> InlineKeyboardMarkup:
    return _look_choices("opacity", [(str(value), f"{value}%") for value in opacities], 4, 1)


def batch_done() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=texts.BTN_BATCH_NEW, callback_data=MenuCallback(action="batch"))
    builder.button(text=texts.BTN_MAIN_MENU_HOME, callback_data=MenuCallback(action="main"))
    builder.adjust(1)
    return builder.as_markup()
