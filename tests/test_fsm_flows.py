"""FSM transition tests for the three flows.

The handlers are invoked directly with lightweight fakes and a real aiogram
``FSMContext`` backed by ``MemoryStorage``, so the assertions are about the
state machine rather than about Telegram transport.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot import texts
from app.bot.callbacks import CircleCallback, MetadataCallback
from app.bot.routers import circle as circle_router
from app.bot.routers import metadata as metadata_router
from app.bot.routers.start import sanitise_source
from app.bot.states import ChangeMetadataStates, CircleStates


class FakeMessage:
    """Records what a handler tried to send."""

    def __init__(self, **fields):
        self.chat = SimpleNamespace(id=1)
        self.from_user = SimpleNamespace(id=42, is_bot=False)
        self.document = None
        self.video = None
        self.photo = None
        self.location = None
        self.answers: list[str] = []
        self.edits: list[str] = []
        self.markups: list = []
        for key, value in fields.items():
            setattr(self, key, value)

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)
        self.markups.append(kwargs.get("reply_markup"))
        return FakeMessage()

    async def edit_text(self, text=None, **kwargs):
        self.edits.append(text)
        return self

    async def delete(self):
        return True


class FakeCallback:
    def __init__(self, message: FakeMessage):
        self.message = message
        self.from_user = SimpleNamespace(id=42, is_bot=False)
        self.answers: list = []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)


@pytest.fixture(autouse=True)
def treat_fakes_as_messages(monkeypatch):
    """Let the ``isinstance(..., Message)`` guards accept our fakes."""
    monkeypatch.setattr(metadata_router, "Message", FakeMessage)
    monkeypatch.setattr(circle_router, "Message", FakeMessage)


@pytest_asyncio.fixture
async def state():
    context = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=42)
    )
    yield context
    await context.clear()


def document(mime_type="image/jpeg", name="IMG_0042.jpg"):
    return SimpleNamespace(
        file_id="FILE-1", file_size=1024, file_name=name, mime_type=mime_type
    )


# --- deep links -------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("promo_x", "promo_x"),
        ("tiktok-2026", "tiktok-2026"),
        ("<script>", "script"),
        ("a" * 100, "a" * 64),
        # Nothing usable survives sanitising, so attribution falls back.
        ("", "direct"),
        (None, "direct"),
        ("!!!", "direct"),
    ],
)
def test_deep_link_source_is_sanitised(raw, expected):
    assert sanitise_source(raw) == expected


@pytest.mark.parametrize(
    "payload",
    ["atreox_channel", "website", "discord", "direct"],
)
def test_the_documented_deep_links_survive_intact(payload):
    assert sanitise_source(payload) == payload


# --- circle -----------------------------------------------------------------


async def test_circle_entry_sets_waiting_state(state):
    callback = FakeCallback(FakeMessage())
    await circle_router.open_circle(callback, state, session=None)
    assert await state.get_state() == CircleStates.waiting_for_video.state
    assert callback.message.edits == [texts.CIRCLE_PROMPT]


async def test_circle_rejects_non_video_without_leaving_the_state(state):
    await state.set_state(CircleStates.waiting_for_video)
    message = FakeMessage()  # no video, no document
    await circle_router.handle_video(
        message, state, bot=None, settings=None, session=None
    )
    assert message.answers == [texts.CIRCLE_WRONG_INPUT]
    assert await state.get_state() == CircleStates.waiting_for_video.state


async def test_forward_help_replaces_the_success_message(state):
    callback = FakeCallback(FakeMessage())
    await circle_router.show_forward_help(callback)
    assert callback.message.edits == [texts.CIRCLE_FORWARD_HELP]
    assert callback.answers == [None]


@pytest.mark.parametrize(
    "handler,expected",
    [
        ("show_ios_guide", texts.CIRCLE_FORWARD_HELP_IOS),
        ("show_android_guide", texts.CIRCLE_FORWARD_HELP_ANDROID),
    ],
)
async def test_platform_guides_render_their_own_copy(handler, expected, state):
    callback = FakeCallback(FakeMessage())
    await getattr(circle_router, handler)(callback)
    assert callback.message.edits == [expected]


async def test_platform_guides_are_distinct_messages():
    # Same steps today, but distinct bodies - otherwise switching platform
    # would be a no-op edit that Telegram rejects.
    assert texts.CIRCLE_FORWARD_HELP_IOS != texts.CIRCLE_FORWARD_HELP_ANDROID
    assert texts.CIRCLE_FORWARD_HELP not in (
        texts.CIRCLE_FORWARD_HELP_IOS,
        texts.CIRCLE_FORWARD_HELP_ANDROID,
    )


async def test_back_returns_to_the_success_message(state):
    callback = FakeCallback(FakeMessage())
    await circle_router.show_forward_help(callback)
    await circle_router.back_to_result(callback)
    assert callback.message.edits == [texts.CIRCLE_FORWARD_HELP, texts.CIRCLE_DONE]


async def test_help_falls_back_to_a_new_message_when_the_edit_fails(state):
    message = FakeMessage()

    async def refuse(text=None, **kwargs):
        raise RuntimeError("message is not modified")

    message.edit_text = refuse
    callback = FakeCallback(message)
    await circle_router.show_forward_help(callback)
    assert message.answers == [texts.CIRCLE_FORWARD_HELP]


async def test_make_another_circle_reenters_the_waiting_state(state):
    # The success keyboard reuses MenuCallback(action="circle"), so pressing it
    # goes through the same entry point as the main menu.
    callback = FakeCallback(FakeMessage())
    await circle_router.open_circle(callback, state, session=None)
    assert await state.get_state() == CircleStates.waiting_for_video.state


# --- change metadata wizard -------------------------------------------------


async def test_wizard_walks_file_device_location_time_confirm(state):
    await state.set_state(ChangeMetadataStates.waiting_for_file)

    message = FakeMessage(document=document())
    await metadata_router.handle_change_file(message, state)
    assert await state.get_state() == ChangeMetadataStates.choosing_generation.state
    assert (await state.get_data())["file"]["file_id"] == "FILE-1"

    # --- device: generation, then variant ---------------------------------
    callback = FakeCallback(FakeMessage())
    await metadata_router.handle_generation(
        callback, MetadataCallback(action="gen", value="15"), state
    )
    assert await state.get_state() == ChangeMetadataStates.choosing_model.state

    await metadata_router.handle_device(
        callback, MetadataCallback(action="device", value="iphone_15_pro"), state
    )
    assert await state.get_state() == ChangeMetadataStates.choosing_country.state
    assert (await state.get_data())["device_key"] == "iphone_15_pro"

    # --- location: country, then city -------------------------------------
    await metadata_router.handle_country(
        callback, MetadataCallback(action="country", value="usa"), state
    )
    assert await state.get_state() == ChangeMetadataStates.choosing_city.state

    await metadata_router.handle_city(
        callback, MetadataCallback(action="city", value="miami"), state
    )
    assert await state.get_state() == ChangeMetadataStates.choosing_time_of_day.state
    assert (await state.get_data())["city_key"] == "miami"

    # --- time of day -------------------------------------------------------
    await metadata_router.handle_time_of_day(
        callback, MetadataCallback(action="tod", value="evening"), state
    )
    assert await state.get_state() == ChangeMetadataStates.confirming.state
    summary = callback.message.edits[-1]
    assert "iPhone 15 Pro" in summary
    assert "Miami" in summary
    assert "Evening" in summary


async def test_wizard_rejects_unsupported_file_and_stays_put(state):
    await state.set_state(ChangeMetadataStates.waiting_for_file)
    message = FakeMessage(document=document(mime_type="application/zip", name="a.zip"))
    await metadata_router.handle_change_file(message, state)
    assert message.answers == [texts.METADATA_WRONG_INPUT]
    assert await state.get_state() == ChangeMetadataStates.waiting_for_file.state


async def test_wizard_warns_when_telegram_compressed_the_upload(state):
    await state.set_state(ChangeMetadataStates.waiting_for_file)
    message = FakeMessage(photo=[SimpleNamespace(file_id="PHOTO-1", file_size=2048)])
    await metadata_router.handle_change_file(message, state)
    assert texts.METADATA_COMPRESSED_WARNING in message.answers
    assert await state.get_state() == ChangeMetadataStates.choosing_generation.state


async def test_unknown_generation_does_not_advance_the_wizard(state):
    await state.set_state(ChangeMetadataStates.choosing_generation)
    callback = FakeCallback(FakeMessage())
    await metadata_router.handle_generation(
        callback, MetadataCallback(action="gen", value="99"), state
    )
    assert await state.get_state() == ChangeMetadataStates.choosing_generation.state


async def test_unknown_device_key_does_not_advance_the_wizard(state):
    await state.set_state(ChangeMetadataStates.choosing_model)
    callback = FakeCallback(FakeMessage())
    await metadata_router.handle_device(
        callback, MetadataCallback(action="device", value="nokia_3310"), state
    )
    assert await state.get_state() == ChangeMetadataStates.choosing_model.state


async def test_unknown_city_does_not_advance_the_wizard(state):
    await state.set_state(ChangeMetadataStates.choosing_city)
    callback = FakeCallback(FakeMessage())
    await metadata_router.handle_city(
        callback, MetadataCallback(action="city", value="atlantis"), state
    )
    assert await state.get_state() == ChangeMetadataStates.choosing_city.state


async def _confirmed_state(state):
    await state.set_state(ChangeMetadataStates.confirming)
    await state.update_data(
        file={"file_id": "FILE-1", "kind": "image"},
        device_key="iphone_14_pro",
        generation=14,
        country_key="usa",
        city_key="miami",
        time_of_day="day",
    )


@pytest.mark.parametrize(
    "value,expected_state",
    [
        ("device", ChangeMetadataStates.choosing_generation),
        ("location", ChangeMetadataStates.choosing_country),
        ("time", ChangeMetadataStates.choosing_time_of_day),
    ],
)
async def test_each_edit_button_reopens_only_its_own_step(value, expected_state, state):
    await _confirmed_state(state)
    callback = FakeCallback(FakeMessage())
    await metadata_router.edit_step(
        callback, MetadataCallback(action="edit", value=value), state
    )

    assert await state.get_state() == expected_state.state
    # The other answers, and the uploaded file, survive.
    data = await state.get_data()
    assert data["file"]["file_id"] == "FILE-1"
    assert data["device_key"] == "iphone_14_pro"
    assert data["city_key"] == "miami"
    assert data["time_of_day"] == "day"


async def test_unknown_edit_target_leaves_the_confirmation_alone(state):
    await _confirmed_state(state)
    callback = FakeCallback(FakeMessage())
    await metadata_router.edit_step(
        callback, MetadataCallback(action="edit", value="nonsense"), state
    )
    assert await state.get_state() == ChangeMetadataStates.confirming.state


async def test_reanswering_the_device_goes_straight_back_to_the_summary(state):
    await _confirmed_state(state)
    await state.set_state(ChangeMetadataStates.choosing_model)

    callback = FakeCallback(FakeMessage())
    await metadata_router.handle_device(
        callback, MetadataCallback(action="device", value="iphone_16_pro_max"), state
    )

    assert await state.get_state() == ChangeMetadataStates.confirming.state
    assert "iPhone 16 Pro Max" in callback.message.edits[-1]


async def test_reanswering_the_city_goes_straight_back_to_the_summary(state):
    await _confirmed_state(state)
    await state.set_state(ChangeMetadataStates.choosing_city)

    callback = FakeCallback(FakeMessage())
    await metadata_router.handle_city(
        callback, MetadataCallback(action="city", value="tokyo"), state
    )

    assert await state.get_state() == ChangeMetadataStates.confirming.state
    summary = callback.message.edits[-1]
    assert "Tokyo" in summary
    # It must not ask for the time again - that answer is still on file.
    assert texts.TIME_OF_DAY_PROMPT not in callback.message.edits


async def test_cancel_clears_the_wizard(state):
    await state.set_state(ChangeMetadataStates.confirming)
    await state.update_data(file={"file_id": "FILE-1"})
    callback = FakeCallback(FakeMessage())
    await metadata_router.cancel(callback, state)
    assert await state.get_state() is None
    assert await state.get_data() == {}


async def test_apply_without_collected_data_fails_safely(state):
    await state.set_state(ChangeMetadataStates.confirming)
    callback = FakeCallback(FakeMessage())
    await metadata_router.apply_changes(
        callback, state, bot=None, settings=None, session=None
    )
    assert await state.get_state() is None
    assert callback.message.answers == [texts.ERROR_GENERIC]


# --- /restart ----------------------------------------------------------------


async def test_restart_drops_everything_the_session_held(state):
    """A wizard half-way through, a collected batch, an uploaded logo: gone."""
    from app.bot.routers import start as start_router

    await state.set_state(ChangeMetadataStates.confirming)
    await state.update_data(
        file={"file_id": "FILE-1"}, logo="Ym9ndXM=", city_key="jp_tokyo"
    )
    message = FakeMessage()
    message.from_user = SimpleNamespace(id=42, is_bot=False)

    await start_router.handle_restart_command(message, state)

    assert await state.get_state() is None
    assert await state.get_data() == {}
    assert message.answers == [texts.RESTARTED]
    # The reply gets the user moving again rather than leaving a dead end.
    labels = [b.text for row in message.markups[-1].inline_keyboard for b in row]
    assert texts.BTN_CIRCLE in labels


def test_restart_copy_is_honest_about_what_it_cannot_do():
    """A bot cannot wipe a chat's history, so the copy must not imply it does."""
    assert "presets are still there" in texts.RESTARTED
    assert "Clear History" in texts.RESTARTED
