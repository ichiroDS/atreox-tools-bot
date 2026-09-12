"""Keyboards for the Watermark flow and its saved presets."""

from __future__ import annotations

from typing import Iterable

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot import texts
from app.bot.callbacks import MenuCallback, WatermarkCallback


def _cancel(builder: InlineKeyboardBuilder) -> None:
    builder.button(text=texts.BTN_CANCEL, callback_data=WatermarkCallback(action="cancel"))


def type_choices() -> InlineKeyboardMarkup:
    """Text, a logo, or something already saved."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=texts.BTN_WATERMARK_TYPE_TEXT,
        callback_data=WatermarkCallback(action="type", value="text"),
    )
    builder.button(
        text=texts.BTN_WATERMARK_TYPE_LOGO,
        callback_data=WatermarkCallback(action="type", value="logo"),
    )
    builder.button(
        text=texts.BTN_WATERMARK_PRESETS, callback_data=WatermarkCallback(action="presets")
    )
    _cancel(builder)
    builder.adjust(2, 1, 1)
    return builder.as_markup()


def text_source_choices() -> InlineKeyboardMarkup:
    """Type a watermark, or reuse a saved one."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=texts.BTN_WATERMARK_ENTER_TEXT, callback_data=WatermarkCallback(action="enter")
    )
    builder.button(
        text=texts.BTN_WATERMARK_PRESETS, callback_data=WatermarkCallback(action="presets")
    )
    _cancel(builder)
    builder.adjust(1)
    return builder.as_markup()


def position_choices() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for value, label in texts.WATERMARK_POSITION_LABELS.items():
        builder.button(text=label, callback_data=WatermarkCallback(action="pos", value=value))
    _cancel(builder)
    builder.adjust(2, 2, 1, 1)
    return builder.as_markup()


def style_choices() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for value, label in texts.WATERMARK_STYLE_LABELS.items():
        builder.button(text=label, callback_data=WatermarkCallback(action="style", value=value))
    _cancel(builder)
    builder.adjust(1)
    return builder.as_markup()


def size_choices() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for value, label in texts.WATERMARK_SIZE_LABELS.items():
        builder.button(text=label, callback_data=WatermarkCallback(action="size", value=value))
    _cancel(builder)
    builder.adjust(3, 1)
    return builder.as_markup()


def opacity_choices(opacities: Iterable[int]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for value in opacities:
        builder.button(
            text=f"{value}%", callback_data=WatermarkCallback(action="opacity", value=str(value))
        )
    _cancel(builder)
    builder.adjust(4, 1)
    return builder.as_markup()


def confirmation_choices() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=texts.BTN_WATERMARK_APPLY, callback_data=WatermarkCallback(action="apply"))
    builder.button(
        text=texts.BTN_WATERMARK_SAVE_PRESET, callback_data=WatermarkCallback(action="save")
    )
    builder.button(
        text=texts.BTN_WATERMARK_CHANGE, callback_data=WatermarkCallback(action="change")
    )
    _cancel(builder)
    builder.adjust(1, 2, 1)
    return builder.as_markup()


def preset_list(presets, *, back_action: str = "back") -> InlineKeyboardMarkup:
    """Every saved watermark, then the ways to add one or step back."""
    builder = InlineKeyboardBuilder()
    for preset in presets:
        builder.button(
            # A logo preset is marked as one, so a list of ten is readable.
            text=f"{'🖼' if getattr(preset, 'is_logo', False) else '💾'} {preset.name}",
            callback_data=WatermarkCallback(action="detail", value=str(preset.id)),
        )
    builder.button(
        text=texts.BTN_WATERMARK_SAVE_NEW, callback_data=WatermarkCallback(action="new")
    )
    builder.button(text=texts.BTN_BACK, callback_data=WatermarkCallback(action=back_action))
    builder.adjust(1)
    return builder.as_markup()


def preset_detail(preset_id: int, *, can_use: bool, is_logo: bool = False) -> InlineKeyboardMarkup:
    """Use it on the file in flight (when there is one), or manage it.

    A saved logo has no text to edit, so that button is simply not offered.
    """
    builder = InlineKeyboardBuilder()
    value = str(preset_id)
    if can_use:
        builder.button(
            text=texts.BTN_WATERMARK_USE, callback_data=WatermarkCallback(action="use", value=value)
        )
    builder.button(
        text=texts.BTN_WATERMARK_RENAME,
        callback_data=WatermarkCallback(action="rename", value=value),
    )
    if not is_logo:
        builder.button(
            text=texts.BTN_WATERMARK_EDIT_TEXT,
            callback_data=WatermarkCallback(action="edit", value=value),
        )
    builder.button(
        text=texts.BTN_WATERMARK_DELETE,
        callback_data=WatermarkCallback(action="delete", value=value),
    )
    builder.button(text=texts.BTN_BACK, callback_data=WatermarkCallback(action="presets"))
    if can_use:
        builder.adjust(1, 2, 1, 1)
    else:
        builder.adjust(2, 1, 1)
    return builder.as_markup()


def delete_confirmation(preset_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=texts.BTN_WATERMARK_DELETE_CONFIRM,
        callback_data=WatermarkCallback(action="delete_yes", value=str(preset_id)),
    )
    builder.button(
        text=texts.BTN_BACK,
        callback_data=WatermarkCallback(action="detail", value=str(preset_id)),
    )
    builder.adjust(1)
    return builder.as_markup()


def watermark_done() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=texts.BTN_WATERMARK_AGAIN, callback_data=MenuCallback(action="watermark")
    )
    builder.button(
        text=texts.BTN_WATERMARK_PRESETS, callback_data=WatermarkCallback(action="presets")
    )
    builder.button(text=texts.BTN_MAIN_MENU_HOME, callback_data=MenuCallback(action="main"))
    builder.adjust(1)
    return builder.as_markup()
