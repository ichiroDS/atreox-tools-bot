"""The GIF / MP4 converter.

Real media throughout: FFmpeg makes the sources, FFmpeg converts them, and
every claim (frame rate, dimensions, silence, duration) is read back out of the
produced file with ffprobe rather than assumed from the arguments.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext

from app.bot import texts
from app.bot.callbacks import AnimationCallback
from app.bot.routers import animation as animation_router
from app.bot.states import AnimationStates
from app.db.models import JobType
from app.services import telegram_files
from app.services.media.animation import (
    CLIP_CHOICE_ABOVE_SECONDS,
    GIF_CLIP_SECONDS,
    GIF_MAX_LONG_SIDE,
    AnimationService,
    Clip,
    ClipChoice,
    Conversion,
    converted_filename,
    parse_animation_source,
    parse_timecode,
    plan_clip,
    plan_gif,
    plan_mp4,
)
from app.services.media.base import MediaProcessingError, ProcessingErrorCode
from app.services.telegram_files import FileKind
from app.utils.temp_files import JobWorkspace
from tests.test_large_files import (
    MB,
    SERVER_FILE,
    FakeBot,
    FakeFileServer,
    FakeJobs,
    local_settings,
    workspaces_left,
)
from tests.test_media_optimizer import (
    FakeCallback,
    FakeMessage,
    fsm_for,
    probe,
    top_level_boxes,
)

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


def service(timeout: float = 300) -> AnimationService:
    return AnimationService(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe",
                            timeout=timeout, probe_timeout=60)


def workspace_in(tmp_path: Path) -> JobWorkspace:
    path = tmp_path / f"ws-{uuid.uuid4().hex[:8]}"
    path.mkdir()
    return JobWorkspace(job_id=uuid.uuid4(), path=path)


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
                   check=True, timeout=300)


@pytest.fixture(scope="module")
def sources(tmp_path_factory):
    """One short video, one long one, one portrait one, and a real GIF."""
    root = tmp_path_factory.mktemp("animation")
    cache: dict[str, Path] = {}

    def get(name: str) -> Path:
        if name in cache:
            return cache[name]
        folder = root / name
        folder.mkdir()
        if name == "short":
            path = folder / "clip.mp4"
            ffmpeg("-f", "lavfi", "-i", "testsrc=size=640x360:rate=30",
                   "-f", "lavfi", "-i", "sine=frequency=440",
                   "-t", "4", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path))
        elif name == "long":
            path = folder / "long.mp4"
            ffmpeg("-f", "lavfi", "-i", "testsrc=size=640x360:rate=30",
                   "-f", "lavfi", "-i", "sine=frequency=440",
                   "-t", "30", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path))
        elif name == "portrait":
            path = folder / "portrait.mp4"
            ffmpeg("-f", "lavfi", "-i", "testsrc=size=540x960:rate=25",
                   "-t", "4", "-pix_fmt", "yuv420p", str(path))
        elif name == "big":
            # Larger than a GIF should ever be: the caps have to bite.
            path = folder / "big.mp4"
            ffmpeg("-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=60",
                   "-t", "5", "-pix_fmt", "yuv420p", str(path))
        elif name == "gif":
            path = folder / "loop.gif"
            ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x240:rate=20", "-t", "3",
                   "-loop", "0", str(path))
        else:  # pragma: no cover - a typo in a test
            raise AssertionError(name)
        cache[name] = path
        return path

    return get


# --- what the file is -------------------------------------------------------------


@needs_ffmpeg
async def test_a_video_offers_gif_and_mp4_and_a_gif_offers_mp4_and_optimize(sources):
    video = await service().analyze(sources("short"))
    gif = await service().analyze(sources("gif"))

    assert video.is_gif is False
    assert video.conversions == (Conversion.TO_GIF, Conversion.TO_MP4)
    assert video.has_audio is True
    assert gif.is_gif is True
    assert gif.conversions == (Conversion.TO_MP4, Conversion.OPTIMIZE_GIF)
    assert gif.has_audio is False


@needs_ffmpeg
async def test_a_gifs_own_pixel_aspect_never_shrinks_it(sources, tmp_path):
    """ffprobe reports 63:64 for a GIF's square pixels; taking that at face
    value shaved a column off every conversion."""
    source = sources("short")
    analysis = await service().analyze(source)
    gif = await service().to_gif(source, workspace_in(tmp_path), analysis, plan_clip(analysis))

    reprobed = await service().analyze(gif.path)
    assert (reprobed.width, reprobed.height) == (gif.width, gif.height)


async def test_an_audio_file_is_refused_before_anything_is_decoded():
    payload = '{"format": {"format_name": "mp3", "duration": "60"}, "streams": []}'
    with pytest.raises(MediaProcessingError) as error:
        parse_animation_source(payload, 1000)
    assert error.value.code is ProcessingErrorCode.UNSUPPORTED


# --- video -> GIF -------------------------------------------------------------------


@needs_ffmpeg
async def test_a_short_video_becomes_a_silent_gif_of_the_whole_clip(sources, tmp_path):
    source = sources("short")
    analysis = await service().analyze(source)
    clip = plan_clip(analysis)
    assert clip == Clip(0.0, analysis.duration)

    result = await service().to_gif(source, workspace_in(tmp_path), analysis, clip)

    written = probe(result.path)
    assert written["video_codec"] == "gif"
    assert written["audio_codecs"] == [], "a GIF cannot carry sound"
    assert result.filename.endswith(".gif")
    assert abs(written["duration"] - analysis.duration) < 0.5


@needs_ffmpeg
async def test_the_gif_keeps_the_aspect_ratio_and_is_never_upscaled(sources, tmp_path):
    for name, size in (("portrait", (540, 960)), ("big", (1920, 1080))):
        analysis = await service().analyze(sources(name))
        result = await service().to_gif(
            sources(name), workspace_in(tmp_path), analysis, plan_clip(analysis)
        )
        assert max(result.width, result.height) <= GIF_MAX_LONG_SIDE
        # Within a pixel: both sides are rounded to even numbers.
        assert abs(result.width / result.height - size[0] / size[1]) < 0.02
        if max(size) <= GIF_MAX_LONG_SIDE:
            assert (result.width, result.height) == size


@needs_ffmpeg
async def test_the_frame_rate_is_capped_so_a_gif_stays_shareable(sources, tmp_path):
    analysis = await service().analyze(sources("big"))       # 60 fps source
    result = await service().to_gif(
        sources("big"), workspace_in(tmp_path), analysis, plan_clip(analysis)
    )
    # A GIF times its frames in hundredths of a second, so 15 fps is stored as
    # the nearest achievable rate rather than exactly 15.
    assert probe(result.path)["fps"] <= 17


@needs_ffmpeg
async def test_a_long_video_is_cut_to_a_clip_rather_than_converted_whole(sources, tmp_path):
    analysis = await service().analyze(sources("long"))
    assert analysis.needs_a_clip is True
    assert analysis.duration > CLIP_CHOICE_ABOVE_SECONDS

    first = plan_clip(analysis, ClipChoice.FIRST)
    middle = plan_clip(analysis, ClipChoice.MIDDLE)
    custom = plan_clip(analysis, ClipChoice.CUSTOM, 20.0)

    assert first == Clip(0.0, GIF_CLIP_SECONDS)
    assert middle.start == pytest.approx((analysis.duration - GIF_CLIP_SECONDS) / 2, abs=0.1)
    assert custom.start == 20.0
    for clip in (first, middle, custom):
        assert clip.end <= analysis.duration + 0.01

    result = await service().to_gif(sources("long"), workspace_in(tmp_path), analysis, middle)
    assert probe(result.path)["duration"] == pytest.approx(GIF_CLIP_SECONDS, abs=0.5)


def test_a_start_time_past_the_end_is_refused():
    analysis = parse_animation_source(
        '{"format": {"format_name": "mov,mp4", "duration": "30"}, "streams":'
        ' [{"codec_type": "video", "codec_name": "h264", "width": 640, "height": 360,'
        ' "index": 0, "avg_frame_rate": "30/1"}]}',
        MB,
    )
    with pytest.raises(MediaProcessingError) as error:
        plan_clip(analysis, ClipChoice.CUSTOM, 45.0)
    assert error.value.code is ProcessingErrorCode.UNSUPPORTED


@pytest.mark.parametrize("raw,expected", [
    ("12", 12.0), ("0", 0.0), ("1:05", 65.0), ("1:05.5", 65.5), ("01:00", 60.0),
    ("", None), ("abc", None), ("-3", None), ("1:75", None), ("1:2:3", None),
    ("99999999999999", None),
])
def test_a_typed_time_is_read_or_refused(raw, expected):
    assert parse_timecode(raw) == expected


# --- GIF -> MP4, and a leaner GIF ----------------------------------------------------


@needs_ffmpeg
async def test_a_gif_becomes_a_silent_faststart_mp4(sources, tmp_path):
    source = sources("gif")
    analysis = await service().analyze(source)
    result = await service().to_mp4(source, workspace_in(tmp_path), analysis)

    written = probe(result.path)
    assert written["video_codec"] == "h264"
    assert written["audio_codecs"] == []
    assert (result.width, result.height) == (320, 240)
    assert written["duration"] == pytest.approx(analysis.duration, abs=0.5)
    assert result.filename.endswith(".mp4")
    # faststart puts the index at the front, which is what lets it stream.
    assert b"moov" in source.with_suffix(".gif").read_bytes()[:0] + \
        result.path.read_bytes()[:2048]


@needs_ffmpeg
async def test_optimizing_a_gif_makes_it_smaller_and_keeps_it_a_gif(sources, tmp_path):
    # A generous GIF, so there is something to save.
    fat = tmp_path / "fat.gif"
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=640x480:rate=25", "-t", "3", str(fat))
    analysis = await service().analyze(fat)

    result = await service().optimize_gif(fat, workspace_in(tmp_path), analysis)

    assert probe(result.path)["video_codec"] == "gif"
    assert result.size_bytes < analysis.size_bytes
    assert probe(result.path)["duration"] == pytest.approx(analysis.duration, abs=0.5)


@needs_ffmpeg
async def test_the_palette_pass_runs_separately_from_the_encode(sources, tmp_path):
    """The one-command form buffers every frame of the clip in memory; this
    build must keep the two passes apart."""
    from app.services.media.animation import build_gif_args, build_palette_args

    analysis = await service().analyze(sources("short"))
    plan = plan_gif(analysis, plan_clip(analysis))
    palette = build_palette_args("ffmpeg", Path("/in.mp4"), Path("/p.png"), plan)
    encode = build_gif_args("ffmpeg", Path("/in.mp4"), Path("/p.png"), Path("/o.gif"), plan)

    assert "palettegen" in " ".join(palette)
    assert "split" not in " ".join(encode)
    assert "paletteuse" in " ".join(encode)
    assert "-an" in encode
    for args in (palette, encode):
        assert args.index("-threads") < args.index("-i")


def test_the_output_name_is_built_from_the_users_filename():
    assert converted_filename("My Holiday.mp4", ".gif") == "atreox_My_Holiday.gif"
    assert converted_filename(None, ".mp4") == "atreox_animation.mp4"
    assert converted_filename("../../etc/passwd", ".gif") == "atreox_passwd.gif"


# --- the flow, through the router ------------------------------------------------


@pytest.fixture(autouse=True)
def treat_fakes_as_messages(monkeypatch):
    monkeypatch.setattr(animation_router, "Message", FakeMessage)


@pytest.fixture
def jobs(monkeypatch):
    recorder = FakeJobs()
    monkeypatch.setattr(animation_router, "JobsRepository", lambda session: recorder)
    return recorder


@pytest.fixture
def file_server(monkeypatch):
    server = FakeFileServer()
    monkeypatch.setattr(telegram_files, "AiohttpFileClient", lambda: server)
    return server


class ConverterMessage(FakeMessage):
    """Also records animations, which is how an MP4 goes back."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.animations: list[dict] = []

    async def answer_animation(self, animation=None, **kwargs):
        path = Path(animation.path)
        self.animations.append({
            "filename": animation.filename, "size": path.stat().st_size,
            "probe": probe(path), **kwargs,
        })
        return SimpleNamespace(animation=SimpleNamespace(file_name=animation.filename))

    async def edit_text(self, text=None, **kwargs):
        self.edits.append(text)
        self.markups.append(kwargs.get("reply_markup"))
        return self


def as_document(path: Path, mime: str, *, size: int | None = None) -> ConverterMessage:
    message = ConverterMessage(keep=None)
    message.document = SimpleNamespace(
        file_id="DOC-1", file_size=size or path.stat().st_size,
        file_name=path.name, mime_type=mime,
    )
    return message


@needs_ffmpeg
async def test_the_whole_flow_from_menu_to_a_gif(sources, tmp_path, clean_env, jobs,
                                                 file_server, session):
    source = sources("short")
    file_server.source = source
    settings = local_settings(tmp_path)
    context = fsm_for()
    message = as_document(source, "video/mp4")
    message.keep = tmp_path / "received"
    bot = FakeBot(SERVER_FILE, size=source.stat().st_size)

    await animation_router.open_animation(FakeCallback(message), context, session=session)
    assert message.edits[-1] == texts.ANIMATION_PROMPT

    await animation_router.handle_media(
        message, context, bot=bot, settings=settings, session=session
    )
    assert await context.get_state() == AnimationStates.choosing_conversion.state
    labels = [b.text for row in message.markups[-1].inline_keyboard for b in row]
    assert labels == [texts.BTN_ANIMATION_TO_GIF, texts.BTN_ANIMATION_TO_MP4, texts.BTN_CANCEL]

    await animation_router.handle_conversion(
        FakeCallback(message), AnimationCallback(action="convert", value="to_gif"),
        context, bot=bot, settings=settings, session=session,
    )

    (document,) = message.documents
    assert document["filename"] == "atreox_clip.gif"
    assert document["probe"]["video_codec"] == "gif"
    assert document["probe"]["audio_codecs"] == []
    assert message.answers[-1] == texts.ANIMATION_GIF_DONE
    assert jobs.created[0]["job_type"] is JobType.CONVERT
    assert jobs.status == "success"
    assert await context.get_state() is None
    assert workspaces_left(settings) == []
    # Fetched twice (once to see what it is, once to convert it) and handed
    # back to the server once the conversion no longer needs it.
    assert file_server.deleted == file_server.requested[-1:]


@needs_ffmpeg
async def test_a_long_video_asks_which_six_seconds(sources, tmp_path, clean_env, jobs,
                                                   file_server, session):
    source = sources("long")
    file_server.source = source
    settings = local_settings(tmp_path)
    context = fsm_for()
    message = as_document(source, "video/mp4")
    bot = FakeBot(SERVER_FILE, size=source.stat().st_size)

    await animation_router.handle_media(
        message, context, bot=bot, settings=settings, session=session
    )
    await animation_router.handle_conversion(
        FakeCallback(message), AnimationCallback(action="convert", value="to_gif"),
        context, bot=bot, settings=settings, session=session,
    )
    assert await context.get_state() == AnimationStates.choosing_clip.state
    labels = [b.text for row in message.markups[-1].inline_keyboard for b in row]
    assert labels == [
        texts.BTN_ANIMATION_CLIP_FIRST, texts.BTN_ANIMATION_CLIP_MIDDLE,
        texts.BTN_ANIMATION_CLIP_CUSTOM, texts.BTN_CANCEL,
    ]
    assert message.documents == [], "nothing is converted before the clip is chosen"

    await animation_router.handle_clip(
        FakeCallback(message), AnimationCallback(action="clip", value="middle"),
        context, bot=bot, settings=settings, session=session,
    )

    (document,) = message.documents
    assert document["probe"]["duration"] == pytest.approx(GIF_CLIP_SECONDS, abs=0.5)
    assert jobs.status == "success"
    assert workspaces_left(settings) == []


@needs_ffmpeg
async def test_a_custom_start_time_is_validated_against_the_video(sources, tmp_path, clean_env,
                                                                  jobs, file_server, session):
    source = sources("long")
    file_server.source = source
    settings = local_settings(tmp_path)
    context = fsm_for()
    message = as_document(source, "video/mp4")
    bot = FakeBot(SERVER_FILE, size=source.stat().st_size)

    await animation_router.handle_media(
        message, context, bot=bot, settings=settings, session=session
    )
    await animation_router.handle_conversion(
        FakeCallback(message), AnimationCallback(action="convert", value="to_gif"),
        context, bot=bot, settings=settings, session=session,
    )
    await animation_router.handle_clip(
        FakeCallback(message), AnimationCallback(action="clip", value="custom"),
        context, bot=bot, settings=settings, session=session,
    )
    assert await context.get_state() == AnimationStates.typing_start_time.state

    # Past the end: refused, and the user stays where they are.
    await animation_router.handle_start_time(
        FakeMessage(text="5:00"), context, bot=bot, settings=settings, session=session
    )
    assert await context.get_state() == AnimationStates.typing_start_time.state
    assert message.documents == []

    await animation_router.handle_start_time(
        ConverterMessage(text="0:10"), context, bot=bot, settings=settings, session=session
    )
    assert jobs.status == "success"


@needs_ffmpeg
async def test_an_mp4_goes_back_as_an_animation(sources, tmp_path, clean_env, jobs,
                                                file_server, session):
    source = sources("gif")
    file_server.source = source
    settings = local_settings(tmp_path)
    context = fsm_for()
    message = as_document(source, "image/gif")
    bot = FakeBot(SERVER_FILE, size=source.stat().st_size)

    await animation_router.handle_media(
        message, context, bot=bot, settings=settings, session=session
    )
    labels = [b.text for row in message.markups[-1].inline_keyboard for b in row]
    assert labels == [
        texts.BTN_ANIMATION_TO_MP4, texts.BTN_ANIMATION_OPTIMIZE_GIF, texts.BTN_CANCEL
    ]

    await animation_router.handle_conversion(
        FakeCallback(message), AnimationCallback(action="convert", value="to_mp4"),
        context, bot=bot, settings=settings, session=session,
    )

    (animation,) = message.animations
    assert animation["filename"] == "atreox_loop.mp4"
    assert animation["probe"]["video_codec"] == "h264"
    assert animation["probe"]["audio_codecs"] == []
    assert message.answers[-1] == texts.ANIMATION_MP4_DONE
    assert message.documents == []
    assert workspaces_left(settings) == []


@needs_ffmpeg
async def test_an_animation_telegram_refuses_is_sent_as_a_file_instead(sources, tmp_path,
                                                                      clean_env, jobs,
                                                                      file_server, session):
    from aiogram.exceptions import TelegramBadRequest

    source = sources("gif")
    file_server.source = source
    settings = local_settings(tmp_path)
    context = fsm_for()
    message = as_document(source, "image/gif")

    async def refuse(animation=None, **kwargs):
        raise TelegramBadRequest(method=SimpleNamespace(), message="ANIMATION_INVALID")

    message.answer_animation = refuse
    bot = FakeBot(SERVER_FILE, size=source.stat().st_size)

    await animation_router.handle_media(
        message, context, bot=bot, settings=settings, session=session
    )
    await animation_router.handle_conversion(
        FakeCallback(message), AnimationCallback(action="convert", value="to_mp4"),
        context, bot=bot, settings=settings, session=session,
    )

    (document,) = message.documents
    assert document["filename"] == "atreox_loop.mp4"
    assert jobs.status == "success", "a refused animation must not fail the job"


@needs_ffmpeg
async def test_an_already_lean_gif_is_left_alone(tmp_path, clean_env, jobs, file_server,
                                                 session, monkeypatch):
    # A GIF the optimizer cannot meaningfully improve: pretend it came back
    # the same size.
    source = tmp_path / "lean.gif"
    ffmpeg("-f", "lavfi", "-i", "color=c=black:s=64x64:r=5", "-t", "1", str(source))
    file_server.source = source
    settings = local_settings(tmp_path)
    context = fsm_for()
    message = as_document(source, "image/gif")
    bot = FakeBot(SERVER_FILE, size=source.stat().st_size)

    await animation_router.handle_media(
        message, context, bot=bot, settings=settings, session=session
    )
    await animation_router.handle_conversion(
        FakeCallback(message), AnimationCallback(action="convert", value="optimize_gif"),
        context, bot=bot, settings=settings, session=session,
    )

    assert message.documents == [], "the original is the better file"
    assert message.answers[-1] == texts.ANIMATION_GIF_ALREADY_SMALL
    assert jobs.status == "success"
    assert jobs.output_size is None
    assert workspaces_left(settings) == []


@needs_ffmpeg
async def test_a_large_file_takes_the_local_bot_api_path(sources, tmp_path, clean_env, jobs,
                                                         file_server, session):
    source = sources("short")
    file_server.source = source
    settings = local_settings(tmp_path)
    context = fsm_for()
    message = as_document(source, "video/mp4", size=300 * MB)
    bot = FakeBot(SERVER_FILE, size=source.stat().st_size)

    await animation_router.handle_media(
        message, context, bot=bot, settings=settings, session=session
    )
    await animation_router.handle_conversion(
        FakeCallback(message), AnimationCallback(action="convert", value="to_gif"),
        context, bot=bot, settings=settings, session=session,
    )

    assert jobs.status == "success", message.answers
    assert bot.downloads == []          # never the cloud download path
    assert file_server.requested and all(
        url.startswith("http://telegram-bot-api.railway.internal:8082/")
        for url in file_server.requested
    )
    assert workspaces_left(settings) == []


@needs_ffmpeg
async def test_a_failed_conversion_cleans_up_and_keeps_the_buttons(sources, tmp_path, clean_env,
                                                                   jobs, file_server, session,
                                                                   monkeypatch):
    source = sources("short")
    file_server.source = source
    settings = local_settings(tmp_path)
    context = fsm_for()
    message = as_document(source, "video/mp4")
    bot = FakeBot(SERVER_FILE, size=source.stat().st_size)

    await animation_router.handle_media(
        message, context, bot=bot, settings=settings, session=session
    )

    async def broken(*args, **kwargs):
        raise MediaProcessingError(ProcessingErrorCode.ENCODE_FAILED, "boom")

    monkeypatch.setattr(AnimationService, "to_gif", broken)
    await animation_router.handle_conversion(
        FakeCallback(message), AnimationCallback(action="convert", value="to_gif"),
        context, bot=bot, settings=settings, session=session,
    )

    assert jobs.status == "failed"
    assert message.documents == []
    # Back on the choice, so the other conversion can be tried.
    assert await context.get_state() == AnimationStates.choosing_conversion.state
    assert workspaces_left(settings) == []


def test_a_file_that_is_not_a_video_or_gif_is_not_accepted():
    from app.services.telegram_files import extract_animation_source

    audio = FakeMessage(document=SimpleNamespace(
        file_id="A", file_size=10, file_name="song.mp3", mime_type="audio/mpeg"))
    assert extract_animation_source(audio) is None

    gif = FakeMessage(document=SimpleNamespace(
        file_id="G", file_size=10, file_name="loop.gif", mime_type="image/gif"))
    incoming = extract_animation_source(gif)
    assert incoming is not None and incoming.kind is FileKind.VIDEO

    animation = FakeMessage(animation=SimpleNamespace(
        file_id="AN", file_size=10, file_name="loop.mp4", mime_type="video/mp4", duration=3))
    assert extract_animation_source(animation).file_id == "AN"
