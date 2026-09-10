"""Keyboards for the Metadata Studio, generated from the preset catalog."""

from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardRemove
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot import texts
from app.bot.callbacks import MenuCallback, MetadataCallback
from app.data.device_presets import (
    GENERATIONS,
    generation_label,
    presets_for_generation,
)
from app.data.locations import COUNTRIES, get_country
from app.services.timeofday import TimeOfDay

TIME_OF_DAY_LABELS: dict[TimeOfDay, str] = {
    time_of_day: texts.time_of_day_button(time_of_day.value) for time_of_day in TimeOfDay
}


def metadata_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=texts.BTN_METADATA_CLEAN, callback_data=MetadataCallback(action="clean")
    )
    builder.button(
        text=texts.BTN_METADATA_CHANGE, callback_data=MetadataCallback(action="change")
    )
    builder.button(text=texts.BTN_MAIN_MENU, callback_data=MenuCallback(action="main"))
    builder.adjust(1)
    return builder.as_markup()


def generation_choices() -> InlineKeyboardMarkup:
    """Step one of picking a device: which iPhone generation."""
    builder = InlineKeyboardBuilder()
    for generation in GENERATIONS:
        builder.button(
            text=f"📱 iPhone {generation_label(generation)}",
            callback_data=MetadataCallback(action="gen", value=str(generation)),
        )
    builder.button(text=texts.BTN_CANCEL, callback_data=MetadataCallback(action="cancel"))
    builder.adjust(2)
    return builder.as_markup()


def model_choices(generation: int) -> InlineKeyboardMarkup:
    """Step two: the variants that generation actually shipped in."""
    builder = InlineKeyboardBuilder()
    for preset in presets_for_generation(generation):
        builder.button(
            text=preset.variant_label,
            callback_data=MetadataCallback(action="device", value=preset.key),
        )
    builder.button(
        text=texts.BTN_BACK, callback_data=MetadataCallback(action="edit", value="device")
    )
    builder.adjust(2)
    return builder.as_markup()


def country_choices() -> InlineKeyboardMarkup:
    """Step one of picking a location: which country."""
    builder = InlineKeyboardBuilder()
    for country in COUNTRIES:
        builder.button(
            text=country.label,
            callback_data=MetadataCallback(action="country", value=country.key),
        )
    builder.button(text=texts.BTN_CANCEL, callback_data=MetadataCallback(action="cancel"))
    builder.adjust(2)
    return builder.as_markup()


def city_choices(country_key: str) -> InlineKeyboardMarkup:
    """Step two: the cities we know coordinates and a timezone for."""
    builder = InlineKeyboardBuilder()
    country = get_country(country_key)
    for city in country.cities if country else ():
        builder.button(
            text=city.label,
            callback_data=MetadataCallback(action="city", value=city.key),
        )
    builder.button(
        text=texts.BTN_BACK,
        callback_data=MetadataCallback(action="edit", value="location"),
    )
    builder.adjust(2)
    return builder.as_markup()


def time_of_day_choices() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for time_of_day, label in TIME_OF_DAY_LABELS.items():
        builder.button(
            text=label,
            callback_data=MetadataCallback(action="tod", value=time_of_day.value),
        )
    builder.button(text=texts.BTN_CANCEL, callback_data=MetadataCallback(action="cancel"))
    builder.adjust(2, 2, 1)
    return builder.as_markup()


def confirmation_choices() -> InlineKeyboardMarkup:
    """Apply, or step back to any single answer without redoing the others."""
    builder = InlineKeyboardBuilder()
    builder.button(text=texts.BTN_APPLY, callback_data=MetadataCallback(action="apply"))
    builder.button(
        text=texts.BTN_CHANGE_DEVICE,
        callback_data=MetadataCallback(action="edit", value="device"),
    )
    builder.button(
        text=texts.BTN_CHANGE_LOCATION,
        callback_data=MetadataCallback(action="edit", value="location"),
    )
    builder.button(
        text=texts.BTN_CHANGE_TIME,
        callback_data=MetadataCallback(action="edit", value="time"),
    )
    builder.button(text=texts.BTN_CANCEL, callback_data=MetadataCallback(action="cancel"))
    builder.adjust(1, 2, 1, 1)
    return builder.as_markup()


def clean_done_choices() -> InlineKeyboardMarkup:
    """Shown with the cleaned file so the next action is one tap away."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=texts.BTN_METADATA_CLEAN_AGAIN,
        callback_data=MetadataCallback(action="clean"),
    )
    builder.button(
        text=texts.BTN_METADATA_CHANGE, callback_data=MetadataCallback(action="change")
    )
    builder.button(text=texts.BTN_MAIN_MENU_HOME, callback_data=MenuCallback(action="main"))
    builder.adjust(1)
    return builder.as_markup()


def change_done_choices() -> InlineKeyboardMarkup:
    """Same idea after a rewrite: go again, or head back to the menu."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=texts.BTN_METADATA_CHANGE, callback_data=MetadataCallback(action="change")
    )
    builder.button(
        text=texts.BTN_METADATA_CLEAN, callback_data=MetadataCallback(action="clean")
    )
    builder.button(text=texts.BTN_MAIN_MENU_HOME, callback_data=MenuCallback(action="main"))
    builder.adjust(1)
    return builder.as_markup()


def remove_reply_keyboard() -> ReplyKeyboardRemove:
    """Clears any reply keyboard an earlier version of the wizard left up."""
    return ReplyKeyboardRemove()
