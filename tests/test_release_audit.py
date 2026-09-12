"""Release audit: properties that must hold across the whole product.

These are not feature tests. They walk the assembled bot - every keyboard, every
router, every user-facing string - and assert the things that quietly rot as a
product grows: a button whose flow was renamed, a state a user can get stuck in,
a counter that stopped being reported, developer wording leaking into copy.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone

import pytest
from aiogram.types import Chat, InlineKeyboardMarkup
from aiogram.types import User as TelegramUser

from app.bot import texts
from app.bot.keyboards import animation as animation_keyboards
from app.bot.keyboards import batch as batch_keyboards
from app.bot.keyboards import circle as circle_keyboards
from app.bot.keyboards import common as common_keyboards
from app.bot.keyboards import frame as frame_keyboards
from app.bot.keyboards import make_sticker as make_sticker_keyboards
from app.bot.keyboards import metadata as metadata_keyboards
from app.bot.keyboards import optimizer as optimizer_keyboards
from app.bot.keyboards import stickers as sticker_keyboards
from app.bot.keyboards import voice as voice_keyboards
from app.bot.keyboards import watermark as watermark_keyboards
from app.bot.routers import (
    admin,
    animation,
    batch,
    circle,
    fallback,
    frame,
    make_sticker,
    metadata,
    optimizer,
    start,
    stickers,
    voice,
    watermark,
)
from app.bot.states import (
    AnimationStates,
    BatchStates,
    ChangeMetadataStates,
    CircleStates,
    CleanMetadataStates,
    FrameStates,
    MakeStickerStates,
    OptimizerStates,
    VoiceStates,
    WatermarkStates,
)
from app.data.locations import COUNTRIES
from app.db.models import Feature, JobType
from app.db.repositories import Stats
from app.services.media.watermark import OPACITIES

USER = TelegramUser(id=42, is_bot=False, first_name="Tester")
CHAT = Chat(id=42, type="private")

PROBED_VIDEO = """{"format": {"format_name": "mov,mp4", "duration": "30"},
 "streams": [{"codec_type": "video", "codec_name": "h264", "width": 3840,
  "height": 2160, "index": 0, "avg_frame_rate": "60/1"}]}"""


ALL_STATE_GROUPS = (
    CircleStates, VoiceStates, OptimizerStates, WatermarkStates, BatchStates,
    AnimationStates, FrameStates, MakeStickerStates,
    CleanMetadataStates, ChangeMetadataStates,
)
ALL_STATES = [None] + [state.state for group in ALL_STATE_GROUPS for state in group.__states__]


def every_keyboard() -> list[tuple[str, InlineKeyboardMarkup]]:
    """Every keyboard the bot can put on screen, with a preset argument where
    one is needed."""
    preset = type("Preset", (), {"id": 7, "name": "@handle"})()
    keyboards: list[tuple[str, InlineKeyboardMarkup]] = [
        ("main_menu", common_keyboards.main_menu()),
        ("back_to_menu", common_keyboards.back_to_menu()),
        ("help_menu", common_keyboards.help_menu()),
        ("privacy_menu", common_keyboards.privacy_menu()),
        ("cta", common_keyboards.cta_keyboard()),
        ("cta_open", common_keyboards.cta_open_keyboard()),
        ("circle_done", circle_keyboards.circle_done()),
        ("long_video", circle_keyboards.long_video_choices()),
        ("circle_forward_help", circle_keyboards.forward_help()),
        ("voice_done", voice_keyboards.voice_done()),
        ("voice_forward_help", voice_keyboards.voice_forward_help()),
        ("optimizer_presets", optimizer_keyboards.preset_choices(["small", "balanced", "high"])),
        ("optimize_done", optimizer_keyboards.optimize_done()),
        ("watermark_type", watermark_keyboards.type_choices()),
        ("watermark_source", watermark_keyboards.text_source_choices()),
        ("watermark_position", watermark_keyboards.position_choices()),
        ("watermark_style", watermark_keyboards.style_choices()),
        ("watermark_size", watermark_keyboards.size_choices()),
        ("watermark_opacity", watermark_keyboards.opacity_choices(OPACITIES)),
        ("watermark_confirm", watermark_keyboards.confirmation_choices()),
        ("watermark_presets", watermark_keyboards.preset_list([preset])),
        ("watermark_preset_detail", watermark_keyboards.preset_detail(7, can_use=True)),
        (
            "watermark_logo_preset_detail",
            watermark_keyboards.preset_detail(7, can_use=True, is_logo=True),
        ),
        ("watermark_delete", watermark_keyboards.delete_confirmation(7)),
        ("watermark_done", watermark_keyboards.watermark_done()),
        ("batch_collecting", batch_keyboards.collecting()),
        ("batch_tools", batch_keyboards.tool_choices()),
        ("batch_watermarks", batch_keyboards.watermark_choices([preset])),
        ("batch_optimizer", batch_keyboards.optimizer_choices()),
        ("batch_position", batch_keyboards.position_choices()),
        ("batch_style", batch_keyboards.style_choices()),
        ("batch_size", batch_keyboards.size_choices()),
        ("batch_opacity", batch_keyboards.opacity_choices(OPACITIES)),
        ("batch_done", batch_keyboards.batch_done()),
        ("metadata_menu", metadata_keyboards.metadata_menu()),
        ("metadata_generations", metadata_keyboards.generation_choices()),
        ("metadata_models", metadata_keyboards.model_choices(16)),
        ("metadata_countries", metadata_keyboards.country_choices()),
        ("metadata_cities", metadata_keyboards.city_choices(COUNTRIES[0].key)),
        ("metadata_time", metadata_keyboards.time_of_day_choices()),
        ("metadata_confirm", metadata_keyboards.confirmation_choices()),
        ("metadata_clean_done", metadata_keyboards.clean_done_choices()),
        ("metadata_change_done", metadata_keyboards.change_done_choices()),
        ("sticker_categories", sticker_keyboards.category_choices()),
        ("sticker_results", sticker_keyboards.result_controls("cute")),
        (
            "animation_conversions",
            animation_keyboards.conversion_choices(["to_gif", "to_mp4", "optimize_gif"]),
        ),
        ("animation_clips", animation_keyboards.clip_choices()),
        ("animation_done", animation_keyboards.animation_done()),
        ("frame_positions", frame_keyboards.position_choices()),
        ("frame_formats", frame_keyboards.format_choices()),
        ("frame_done", frame_keyboards.frame_done()),
        ("sticker_styles", make_sticker_keyboards.style_choices()),
        ("make_sticker_done", make_sticker_keyboards.sticker_done()),
    ]
    return keyboards


def callback_payloads() -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []
    for name, markup in every_keyboard():
        for row in markup.inline_keyboard:
            for button in row:
                if button.callback_data:
                    seen.append((name, button.callback_data))
    return seen


async def _matches(handler, event, raw_state) -> bool:
    """Whether one registered handler would accept this event in this state."""
    for filter_object in handler.filters or []:
        try:
            result = filter_object.call(event, raw_state=raw_state, event_from_user=USER)
            if inspect.isawaitable(result):
                result = await result
        except TypeError:
            return False
        if not result:
            return False
    return True


# The routers are module singletons; the tree itself may only be assembled
# once per process (tests/test_wiring.py is that caller), so the audit walks
# the same routers in dispatch order without attaching them again.
ROUTERS = (
    start.router, circle.router, voice.router, optimizer.router, watermark.router,
    batch.router, animation.router, frame.router, make_sticker.router,
    metadata.router, stickers.router, admin.router, fallback.router,
)


async def routes(event, *, states=ALL_STATES) -> list[str]:
    """Which handlers would take this event, in any state."""
    found = []
    for router in ROUTERS:
        handlers = (
            router.callback_query.handlers
            if event.__class__.__name__ == "CallbackQuery"
            else router.message.handlers
        )
        for handler in handlers:
            for raw_state in states:
                if await _matches(handler, event, raw_state):
                    found.append(f"{router.name}.{handler.callback.__name__}")
                    break
    return found


def callback(data: str):
    from aiogram.types import CallbackQuery

    return CallbackQuery(id="1", from_user=USER, chat_instance="c", data=data)


def message(text: str):
    from aiogram.types import Message

    return Message(message_id=1, date=datetime.now(timezone.utc), chat=CHAT, from_user=USER,
                   text=text)


# --- no dead buttons, no dead ends ------------------------------------------------


@pytest.mark.parametrize("origin,payload", callback_payloads(),
                         ids=[f"{name}:{data}" for name, data in callback_payloads()])
async def test_every_button_reaches_a_handler(origin, payload):
    """A button whose flow was renamed would silently do nothing."""
    assert await routes(callback(payload)), f"{origin} button {payload!r} routes nowhere"


@pytest.mark.parametrize("state", ALL_STATES, ids=lambda s: (s or "no-state").split(":")[-1])
async def test_no_state_is_a_dead_end(state):
    """From anywhere, Main Menu and /start must always get the user out."""
    assert await routes(callback("menu:main"), states=[state]), state
    assert await routes(message("/start"), states=[state]), state
    assert await routes(message("/help"), states=[state]), state
    assert await routes(message("/cancel"), states=[state]), state


@pytest.mark.parametrize("state", ALL_STATES, ids=lambda s: (s or "no-state").split(":")[-1])
async def test_any_stray_message_is_answered_in_every_state(state):
    """No state may swallow a message without replying - that reads as a
    frozen bot."""
    assert await routes(message("hello there"), states=[state]), state


def test_every_cancel_button_points_at_a_cancel_handler():
    cancels = {
        payload for _, payload in callback_payloads()
        if payload.endswith(":cancel") or ":cancel:" in payload
    }
    # Every flow that shows a Cancel uses its own namespace, so the cancel
    # lands in the flow that owns the state it has to clear.
    assert {payload.split(":")[0] for payload in cancels} == {
        "crc", "meta", "wm", "opt", "bt", "anm", "frm", "mks",
    }


# --- one product, one vocabulary ---------------------------------------------------


def user_facing_strings() -> dict[str, str]:
    return {
        name: value for name, value in vars(texts).items()
        if isinstance(value, str) and not name.startswith("_") and name.isupper()
    }


@pytest.mark.parametrize(
    "term",
    ["ffmpeg", "exiftool", "pillow", "libx264", "libopus", "drawtext", "fsm", "workspace",
     "subprocess", "traceback", "stacktrace", "sqlalchemy", "postgres", "alembic",
     "bot api", "getfile", "sendvoice", "senddocument", "unlimited", "none", "null"],
)
def test_no_developer_wording_reaches_the_user(term):
    offenders = [
        name for name, value in user_facing_strings().items() if term in value.lower()
    ]
    assert not offenders, f"{term!r} appears in {offenders}"


def test_every_tool_in_the_menu_is_explained_in_help():
    labels = [
        button.text for row in common_keyboards.main_menu().inline_keyboard for button in row
    ]
    tools = [label for label in labels if label not in (texts.BTN_GROW, texts.BTN_HELP)]
    for label in tools:
        name = label.split(" ", 1)[1]
        assert name in texts.HELP, f"{name} is in the menu but not in /help"


def test_the_menu_reads_as_pairs_not_one_long_column():
    rows = common_keyboards.main_menu().inline_keyboard
    assert [len(row) for row in rows] == [2, 2, 2, 2, 2, 2]
    assert [button.text for button in rows[0]] == [texts.BTN_CIRCLE, texts.BTN_VOICE]
    assert [button.text for button in rows[1]] == [texts.BTN_METADATA, texts.BTN_OPTIMIZE]
    assert [button.text for button in rows[2]] == [texts.BTN_WATERMARK, texts.BTN_BATCH]
    assert [button.text for button in rows[3]] == [texts.BTN_ANIMATION, texts.BTN_FRAME]
    assert [button.text for button in rows[4]] == [texts.BTN_MAKE_STICKER, texts.BTN_STICKERS]
    assert [button.text for button in rows[5]] == [texts.BTN_GROW, texts.BTN_HELP]


def test_help_and_the_menu_list_the_tools_in_the_same_order():
    labels = [
        button.text for row in common_keyboards.main_menu().inline_keyboard for button in row
    ]
    tools = [label.split(" ", 1)[1] for label in labels
             if label not in (texts.BTN_GROW, texts.BTN_HELP)]
    positions = [texts.HELP.index(name) for name in tools]
    assert positions == sorted(positions)


def test_privacy_describes_what_is_actually_kept():
    # The one piece of user content the bot stores on purpose.
    assert "presets" in texts.PRIVACY.lower()
    assert "File contents are never stored." in texts.PRIVACY


# --- analytics ----------------------------------------------------------------------


def test_every_job_type_is_reported_in_stats():
    report = texts.stats_report(Stats())
    expected = {
        JobType.CIRCLE: "Circles",
        JobType.VOICE_NOTE: "Voice notes",
        JobType.MEDIA_OPTIMIZE: "Optimizations",
        JobType.WATERMARK: "Watermarks",
        JobType.METADATA_CLEAN: "Metadata cleans",
        JobType.METADATA_CHANGE: "Metadata changes",
        JobType.CONVERT: "GIF/MP4 conversions",
        JobType.MAKE_STICKER: "Stickers made",
        JobType.EXTRACT_FRAME: "Frames extracted",
    }
    for job_type, label in expected.items():
        assert label in report, f"{job_type.value} is processed but never reported"
    assert "Batches" in report
    assert "Sticker searches" in report
    assert "CTA clicks" in report


def test_stats_counts_each_thing_once():
    report = texts.stats_report(Stats())
    for label in ("Circles:", "Voice notes:", "Optimizations:", "Watermarks:", "Batches:"):
        assert report.count(label) == 1


def test_the_system_footer_is_operational_not_a_dashboard():
    footer = texts.stats_system(
        version="abc1234", database="connected", local_api=True, active_jobs=1, job_slots=2
    )
    assert "Version: abc1234" in footer
    assert "Local Bot API: on" in footer
    assert "Active media jobs: 1/2" in footer


def test_feature_and_job_names_fit_their_columns():
    assert all(len(feature.value) <= 32 for feature in Feature)
    assert all(len(job_type.value) <= 32 for job_type in JobType)

# --- resource safety ----------------------------------------------------------------


def test_every_ffmpeg_invocation_states_a_thread_budget():
    """Left automatic, FFmpeg sizes its pools from the host's CPU count - which
    in a 1 GB container is how an encode gets OOM-killed."""
    from pathlib import Path as _Path

    from app.services.media.animation import (
        Clip,
        build_gif_args,
        build_mp4_args,
        build_palette_args,
        parse_animation_source,
        plan_gif,
        plan_mp4,
    )
    from app.services.media.base import MAX_ENCODE_THREADS
    from app.services.media.circle import build_circle_ffmpeg_args
    from app.services.media.frame import Position as FramePosition
    from app.services.media.frame import build_frame_args, plan_frame
    from app.services.media.optimizer import VideoAnalysis, build_video_args, plan_video
    from app.services.media.optimizer import Preset
    from app.services.media.voice import build_voice_ffmpeg_args
    from app.services.media.watermark import WatermarkSpec, resolve_font
    from app.services.media.watermark import build_video_args as build_watermark_args
    from app.services.media.watermark import plan_video as plan_watermark

    source, destination = _Path("/in.mp4"), _Path("/out.mp4")
    analysis = VideoAnalysis(
        size_bytes=10 ** 8, width=3840, height=2160, duration=30.0, video_codec="h264",
        video_stream=0, frame_rate=60.0, video_bitrate=40_000_000, bitrate_declared=True,
    )
    spec = WatermarkSpec("@handle")
    animation_source = parse_animation_source(PROBED_VIDEO, 10 ** 8)
    gif_plan = plan_gif(animation_source, Clip(0.0, 6.0))
    builds = {
        "circle": build_circle_ffmpeg_args("ffmpeg", source, destination, size=384,
                                           duration_limit=60),
        "voice": build_voice_ffmpeg_args("ffmpeg", source, destination),
        "optimizer": build_video_args("ffmpeg", source, destination, analysis,
                                      plan_video(analysis, Preset.BALANCED)),
        "watermark": build_watermark_args("ffmpeg", source, destination, analysis,
                                          plan_watermark(analysis, spec, resolve_font()), spec),
        "gif palette": build_palette_args("ffmpeg", source, _Path("/p.png"), gif_plan),
        "gif": build_gif_args("ffmpeg", source, _Path("/p.png"), _Path("/out.gif"), gif_plan),
        "animation mp4": build_mp4_args("ffmpeg", source, destination, animation_source,
                                        plan_mp4(animation_source)),
        "frame": build_frame_args("ffmpeg", source, _Path("/out.jpg"), analysis,
                                  plan_frame(analysis, FramePosition.MIDDLE, "jpeg")),
    }
    for name, args in builds.items():
        assert "-threads" in args, f"{name} lets FFmpeg choose its own thread count"
        budget = int(args[args.index("-threads") + 1])
        assert 1 <= budget <= MAX_ENCODE_THREADS, f"{name} asks for {budget} threads"
        # The decoder is bounded too, not just the encoder.
        assert args.index("-threads") < args.index("-i"), f"{name} bounds only the encoder"


# --- failure recovery -----------------------------------------------------------------


async def test_jobs_left_running_by_a_restart_are_closed_at_startup(session):
    """A deploy or an OOM kill stops a job mid-flight; the row must not sit in
    "processing" forever."""
    import uuid as _uuid

    from app.db.models import Job, JobStatus
    from app.db.repositories import JobsRepository
    from sqlalchemy import select as _select

    repository = JobsRepository(session)
    running = await repository.create(job_id=_uuid.uuid4(), telegram_user_id=1,
                                      job_type=JobType.CIRCLE)
    finished = await repository.create(job_id=_uuid.uuid4(), telegram_user_id=1,
                                       job_type=JobType.WATERMARK)
    await repository.mark_success(finished, output_size=10)

    closed = await repository.fail_interrupted()

    assert closed == 1
    rows = {row.id: row for row in (await session.execute(_select(Job))).scalars()}
    assert rows[running.id].status == JobStatus.FAILED.value
    assert rows[running.id].error_code == "interrupted"
    assert rows[running.id].finished_at is not None
    # A job that had already finished is left exactly as it was.
    assert rows[finished.id].status == JobStatus.SUCCESS.value
    assert rows[finished.id].output_size == 10
    # Running it again on a clean slate closes nothing.
    assert await repository.fail_interrupted() == 0


# --- security ---------------------------------------------------------------------------


def source_files():
    from pathlib import Path as _Path

    return [path for path in _Path("app").rglob("*.py")] + [
        path for path in _Path("scripts").rglob("*.py")
    ]


def test_no_shell_is_ever_spawned():
    """User filenames and watermark text reach argv, never a shell string."""
    offenders = []
    for path in source_files():
        text = path.read_text(encoding="utf-8")
        for marker in ("shell=True", "os.system(", "os.popen(", "commands.getoutput"):
            if marker in text:
                offenders.append(f"{path}: {marker}")
    assert not offenders, offenders


def test_subprocesses_only_start_through_the_guarded_helpers():
    """Every tool invocation must carry a timeout and be killed when it expires."""
    guarded_helper = ("app", "utils", "subprocess.py")
    offenders = []
    for path in source_files():
        if path.parts[-3:] == guarded_helper or path.name in (
            "image_worker.py", "watermark_worker.py",
        ):
            continue                  # the workers are *started* by the helpers
        text = path.read_text(encoding="utf-8")
        if "subprocess.run(" in text or "subprocess.Popen(" in text:
            offenders.append(str(path))
        if "create_subprocess_exec" in text and "utils" not in str(path):
            offenders.append(str(path))
    assert not offenders, offenders


def test_admin_only_surfaces_are_behind_the_admin_filter():
    from app.bot.routers import admin as admin_router

    guarded = [handler for handler in admin_router.router.message.handlers]
    assert guarded, "the admin router registers nothing"
    for handler in guarded:
        filters = [type(f.callback).__name__ for f in handler.filters or []]
        assert "IsAdmin" in filters, f"{handler.callback.__name__} is not admin-only"
