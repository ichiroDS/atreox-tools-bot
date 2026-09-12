"""Smoke tests that the bot actually assembles: routers, keyboards, API server."""

from __future__ import annotations

import pytest

from app.bot import texts
from app.bot.callbacks import (
    CircleCallback,
    MenuCallback,
    MetadataCallback,
    OptimizeCallback,
    StickerCallback,
    VoiceCallback,
)
from app.bot.keyboards.circle import circle_done, forward_help
from app.bot.keyboards.optimizer import optimize_done, preset_choices
from app.bot.keyboards.voice import voice_done, voice_forward_help
from app.bot.keyboards.common import (
    back_to_menu,
    cta_keyboard,
    cta_open_keyboard,
    help_menu,
    main_menu,
    privacy_menu,
)
from app.bot.keyboards.metadata import (
    change_done_choices,
    city_choices,
    clean_done_choices,
    confirmation_choices,
    country_choices,
    generation_choices,
    metadata_menu,
    model_choices,
    time_of_day_choices,
)
from app.bot.keyboards.stickers import category_choices, result_controls
from app.bot.middlewares import DbSessionMiddleware, UserMiddleware
from app.bot.texts import (
    BTN_BATCH,
    BTN_CIRCLE,
    BTN_HELP,
    BTN_METADATA,
    BTN_OPTIMIZE,
    BTN_STICKERS,
    BTN_VOICE,
    BTN_WATERMARK,
)
from app.config import Settings
from app.data.device_presets import (
    DEVICE_PRESETS,
    GENERATIONS,
    generation_label,
    presets_for_generation,
)
from app.data.locations import COUNTRIES
from app.services.timeofday import TimeOfDay
from app.main import create_bot, create_dispatcher
from app.services.stickers.catalog import STICKER_CATEGORIES

FAKE_TOKEN = "123456789:AAFakeTokenForTestsOnly-0123456789abcdef"


def test_dispatcher_wires_every_flow_with_the_fallback_last(clean_env):
    # Routers are module-level singletons, so the tree may only be built once
    # per process - this test is the single caller.
    dispatcher = create_dispatcher(Settings(bot_token=FAKE_TOKEN), None)
    assert dispatcher["settings"].bot_token == FAKE_TOKEN

    root = dispatcher.sub_routers[0]
    names = [r.name for r in root.sub_routers]
    assert names == [
        "start", "circle", "voice", "optimizer", "watermark", "batch", "animation",
        "frame", "make_sticker", "metadata", "stickers", "admin", "fallback",
    ]
    installed = {type(m) for m in dispatcher.update.outer_middleware}
    assert {DbSessionMiddleware, UserMiddleware} <= installed


def test_bot_targets_the_configured_api_server(clean_env):
    settings = Settings(bot_token=FAKE_TOKEN, bot_api_base_url="http://bot-api:8081")
    bot = create_bot(settings)
    assert "bot-api:8081" in bot.session.api.base


def test_main_menu_lists_the_tools_growth_and_help():
    labels = [b.text for row in main_menu().inline_keyboard for b in row]
    assert labels == [
        BTN_CIRCLE,
        BTN_VOICE,
        BTN_METADATA,
        BTN_OPTIMIZE,
        BTN_WATERMARK,
        BTN_BATCH,
        texts.BTN_ANIMATION,
        texts.BTN_FRAME,
        texts.BTN_MAKE_STICKER,
        BTN_STICKERS,
        texts.BTN_GROW,
        BTN_HELP,
    ]


def test_grow_my_channel_opens_atreox_directly():
    grow = [
        b
        for row in main_menu().inline_keyboard
        for b in row
        if b.text == texts.BTN_GROW
    ][0]
    # A plain link: one tap, no interstitial.
    assert grow.url == "https://atreoxai.com"


def test_help_offers_privacy_atreox_and_a_way_home():
    buttons = [b for row in help_menu().inline_keyboard for b in row]
    assert [b.text for b in buttons] == [
        texts.BTN_PRIVACY,
        texts.BTN_ATREOX,
        texts.BTN_MAIN_MENU_HOME,
    ]
    assert buttons[1].url == texts.ATREOX_URL


def test_privacy_screen_can_go_back_and_home():
    labels = [b.text for row in privacy_menu().inline_keyboard for b in row]
    assert labels == [texts.BTN_BACK, texts.BTN_MAIN_MENU_HOME]


def test_cta_button_is_a_callback_so_the_tap_can_be_counted():
    buttons = [b for row in cta_keyboard().inline_keyboard for b in row]
    assert len(buttons) == 1
    assert buttons[0].callback_data == MenuCallback(action="cta").pack()
    assert buttons[0].url is None
    # ...and the follow-up hands over the real link.
    opener = [b for row in cta_open_keyboard().inline_keyboard for b in row][0]
    assert opener.url == texts.ATREOX_URL


def test_public_command_menu_hides_the_admin_command():
    from app.main import PUBLIC_COMMANDS

    commands = {c.command for c in PUBLIC_COMMANDS}
    assert commands == {"start", "help", "privacy"}
    assert "stats" not in commands
    for command in PUBLIC_COMMANDS:
        assert command.description


@pytest.mark.parametrize(
    "markup",
    [
        main_menu(),
        back_to_menu(),
        help_menu(),
        privacy_menu(),
        cta_keyboard(),
        cta_open_keyboard(),
        circle_done(),
        forward_help(),
        voice_done(),
        voice_forward_help(),
        preset_choices(["small", "balanced", "high"], {"small": 18 * 1024 * 1024}),
        optimize_done(),
        metadata_menu(),
        clean_done_choices(),
        change_done_choices(),
        generation_choices(),
        model_choices(16),
        country_choices(),
        city_choices("usa"),
        time_of_day_choices(),
        confirmation_choices(),
        category_choices(),
        result_controls("cute"),
    ],
)
def test_callback_payloads_fit_telegram_budget(markup):
    for row in markup.inline_keyboard:
        for button in row:
            # A link button carries a URL instead of a payload.
            assert button.callback_data or button.url
            if button.callback_data:
                assert len(button.callback_data.encode("utf-8")) <= 64


def test_circle_done_offers_help_retry_and_menu():
    labels = [b.text for row in circle_done().inline_keyboard for b in row]
    assert labels == [
        texts.BTN_CIRCLE_FORWARD_HELP,
        texts.BTN_CIRCLE_AGAIN,
        texts.BTN_MAIN_MENU_HOME,
    ]
    payloads = [b.callback_data for row in circle_done().inline_keyboard for b in row]
    # Retry and menu reuse the existing entry points rather than new handlers.
    assert payloads[1] == MenuCallback(action="circle").pack()
    assert payloads[2] == MenuCallback(action="main").pack()


def test_forward_help_keyboard_keeps_a_callback_per_platform():
    buttons = [b for row in forward_help().inline_keyboard for b in row]
    assert [b.text for b in buttons] == [
        texts.BTN_GUIDE_IOS,
        texts.BTN_GUIDE_ANDROID,
        texts.BTN_BACK,
    ]
    # Separate payloads today so per-platform guides can diverge later.
    assert buttons[0].callback_data != buttons[1].callback_data
    assert buttons[2].callback_data == CircleCallback(action="forward_back").pack()


def test_metadata_menu_offers_clean_change_and_the_main_menu():
    labels = [b.text for row in metadata_menu().inline_keyboard for b in row]
    assert labels == [
        texts.BTN_METADATA_CLEAN,
        texts.BTN_METADATA_CHANGE,
        texts.BTN_MAIN_MENU,
    ]


def test_generation_keyboard_lists_every_generation():
    labels = [b.text for row in generation_choices().inline_keyboard for b in row]
    for generation in GENERATIONS:
        assert f"📱 iPhone {generation_label(generation)}" in labels
    assert len(labels) == len(GENERATIONS) + 1  # plus Cancel


@pytest.mark.parametrize("generation", GENERATIONS)
def test_model_keyboard_lists_that_generation_only(generation):
    buttons = [b for row in model_choices(generation).inline_keyboard for b in row]
    keys = [
        MetadataCallback.unpack(b.callback_data).value
        for b in buttons
        if MetadataCallback.unpack(b.callback_data).action == "device"
    ]
    assert keys == [p.key for p in presets_for_generation(generation)]
    # Every generation offers a way back to the generation list.
    assert buttons[-1].text == texts.BTN_BACK


def test_every_preset_is_reachable_through_the_two_steps():
    reachable = {
        p.key for generation in GENERATIONS for p in presets_for_generation(generation)
    }
    assert reachable == {p.key for p in DEVICE_PRESETS}


def test_country_keyboard_lists_every_country():
    labels = [b.text for row in country_choices().inline_keyboard for b in row]
    for country in COUNTRIES:
        assert country.label in labels


@pytest.mark.parametrize("country", COUNTRIES, ids=lambda c: c.key)
def test_city_keyboard_lists_that_country_only(country):
    buttons = [b for row in city_choices(country.key).inline_keyboard for b in row]
    keys = [
        MetadataCallback.unpack(b.callback_data).value
        for b in buttons
        if MetadataCallback.unpack(b.callback_data).action == "city"
    ]
    assert keys == [c.key for c in country.cities]


def test_confirmation_offers_apply_and_a_way_back_to_each_answer():
    buttons = [b for row in confirmation_choices().inline_keyboard for b in row]
    assert [b.text for b in buttons] == [
        texts.BTN_APPLY,
        texts.BTN_CHANGE_DEVICE,
        texts.BTN_CHANGE_LOCATION,
        texts.BTN_CHANGE_TIME,
        texts.BTN_CANCEL,
    ]
    edits = [MetadataCallback.unpack(b.callback_data) for b in buttons[1:4]]
    # Each edit button targets exactly one step.
    assert [e.value for e in edits] == ["device", "location", "time"]


def test_clean_result_keyboard_offers_the_obvious_next_steps():
    labels = [b.text for row in clean_done_choices().inline_keyboard for b in row]
    assert labels == [
        texts.BTN_METADATA_CLEAN_AGAIN,
        texts.BTN_METADATA_CHANGE,
        texts.BTN_MAIN_MENU_HOME,
    ]


def test_time_of_day_keyboard_covers_every_interval():
    labels = [b.text for row in time_of_day_choices().inline_keyboard for b in row]
    for time_of_day in TimeOfDay:
        assert texts.time_of_day_button(time_of_day.value) in labels


def test_sticker_keyboard_covers_every_configured_category():
    labels = [b.text for row in category_choices().inline_keyboard for b in row]
    for category in STICKER_CATEGORIES:
        assert category.label in labels


@pytest.mark.parametrize(
    "factory,payload",
    [
        (MenuCallback, {"action": "circle"}),
        (CircleCallback, {"action": "forward_help"}),
        (CircleCallback, {"action": "guide_android"}),
        (MenuCallback, {"action": "voice"}),
        (VoiceCallback, {"action": "forward_help"}),
        (MenuCallback, {"action": "optimize"}),
        (OptimizeCallback, {"action": "preset", "value": "balanced"}),
        (MetadataCallback, {"action": "device", "value": "iphone_16_pro_max"}),
        (MetadataCallback, {"action": "gen", "value": "16"}),
        (MetadataCallback, {"action": "city", "value": "los_angeles"}),
        (MetadataCallback, {"action": "edit", "value": "location"}),
        (StickerCallback, {"action": "category", "category": "morning_night"}),
    ],
)
def test_callback_data_round_trips(factory, payload):
    packed = factory(**payload).pack()
    assert factory.unpack(packed) == factory(**payload)


async def test_admin_filter_matches_only_configured_admins(clean_env):
    from types import SimpleNamespace

    from app.bot.routers.admin import IsAdmin

    is_admin = IsAdmin()
    settings = Settings(bot_token=FAKE_TOKEN, admin_user_ids="42")
    admin = SimpleNamespace(from_user=SimpleNamespace(id=42))
    stranger = SimpleNamespace(from_user=SimpleNamespace(id=7))

    assert await is_admin(admin, settings=settings) is True
    # A non-admin does not match, so their sticker falls through to the
    # fallback router instead of being silently swallowed.
    assert await is_admin(stranger, settings=settings) is False
    assert await is_admin(admin) is False
