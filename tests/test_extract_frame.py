"""Extract Frame: one still out of a video, at full size and the right way up.

The pixel dimensions and the moment are both read back from the produced file.
A rotated source is the interesting case: the still has to come out upright,
which means its width and height swap relative to how the video is stored.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.bot import texts
from app.bot.callbacks import FrameCallback
from app.bot.routers import frame as frame_router
from app.bot.states import FrameStates
from app.db.models import JobType
from app.services import telegram_files
from app.services.media.base import MediaProcessingError, ProcessingErrorCode
from app.services.media.frame import (
    FrameService,
    Position,
    frame_filename,
    plan_frame,
    seek_seconds,
)
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
from tests.test_media_optimizer import FakeCallback, FakeMessage, fsm_for, probe

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


def service(timeout: float = 300) -> FrameService:
    return FrameService(ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe",
                        timeout=timeout, probe_timeout=60)


def workspace_in(tmp_path: Path) -> JobWorkspace:
    path = tmp_path / f"ws-{uuid.uuid4().hex[:8]}"
    path.mkdir()
    return JobWorkspace(job_id=uuid.uuid4(), path=path)


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
                   check=True, timeout=300)


@pytest.fixture(scope="module")
def videos(tmp_path_factory):
    root = tmp_path_factory.mktemp("frames")
    cache: dict[str, Path] = {}

    def get(name: str) -> Path:
        if name in cache:
            return cache[name]
        folder = root / name
        folder.mkdir()
        if name == "landscape":
            path = folder / "clip.mp4"
            ffmpeg("-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30",
                   "-t", "8", "-pix_fmt", "yuv420p", str(path))
        elif name == "portrait":
            path = folder / "vertical.mp4"
            ffmpeg("-f", "lavfi", "-i", "testsrc=size=720x1280:rate=30",
                   "-t", "6", "-pix_fmt", "yuv420p", str(path))
        elif name == "rotated":
            # Stored landscape, displayed portrait: a phone video held upright.
            # The display matrix is what players honour, so it is set that way.
            plain = folder / "plain.mp4"
            ffmpeg("-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30", "-t", "5",
                   "-pix_fmt", "yuv420p", str(plain))
            path = folder / "IMG_1234.mov"
            ffmpeg("-display_rotation:v:0", "90", "-i", str(plain), "-c", "copy", str(path))
        elif name == "quicktime":
            path = folder / "shot.mov"
            ffmpeg("-f", "lavfi", "-i", "testsrc=size=640x480:rate=25", "-t", "4",
                   "-pix_fmt", "yuv420p", "-c:v", "libx264", str(path))
        else:  # pragma: no cover - a typo in a test
            raise AssertionError(name)
        cache[name] = path
        return path

    return get


# --- where in the video ------------------------------------------------------------


@pytest.mark.parametrize("position,expected", [
    (Position.FIRST, 0.0),
    (Position.QUARTER, 5.0),
    (Position.MIDDLE, 10.0),
    (Position.THREE_QUARTER, 15.0),
])
def test_each_position_maps_to_the_moment_it_names(position, expected):
    assert seek_seconds(position, 20.0) == expected


def test_a_custom_time_must_be_inside_the_video():
    assert seek_seconds(Position.CUSTOM, 20.0, 12.5) == 12.5
    for bad in (-1.0, 25.0, None):
        with pytest.raises(MediaProcessingError) as error:
            seek_seconds(Position.CUSTOM, 20.0, bad)
        assert error.value.code is ProcessingErrorCode.UNSUPPORTED


def test_the_very_last_instant_is_stepped_back_from():
    """A seek to exactly the end lands past the final frame."""
    assert seek_seconds(Position.CUSTOM, 10.0, 10.0) < 10.0
    assert seek_seconds(Position.THREE_QUARTER, 10.0) == 7.5


# --- the still itself ---------------------------------------------------------------


@needs_ffmpeg
@pytest.mark.parametrize("image_format,codec", [("jpeg", "mjpeg"), ("png", "png")])
async def test_a_frame_comes_out_in_the_chosen_format(image_format, codec, videos, tmp_path):
    source = videos("landscape")
    analysis = await service().analyze(source)
    result = await service().extract(
        source, workspace_in(tmp_path), analysis, Position.MIDDLE, image_format
    )

    written = probe(result.path)
    assert written["video_codec"] == codec
    assert (written["width"], written["height"]) == (1280, 720)
    assert result.image_format == image_format
    assert result.seconds == pytest.approx(analysis.duration / 2, abs=0.1)


@needs_ffmpeg
@pytest.mark.parametrize("name,size", [
    ("landscape", (1280, 720)),
    ("portrait", (720, 1280)),
    ("quicktime", (640, 480)),
])
async def test_the_still_keeps_the_exact_frame_size(name, size, videos, tmp_path):
    source = videos(name)
    analysis = await service().analyze(source)
    result = await service().extract(
        source, workspace_in(tmp_path), analysis, Position.FIRST, "png"
    )

    written = probe(result.path)
    assert (written["width"], written["height"]) == size
    assert (result.width, result.height) == size


@needs_ffmpeg
async def test_a_rotated_video_gives_an_upright_still(videos, tmp_path):
    source = videos("rotated")
    analysis = await service().analyze(source)
    # Stored 1280x720, displayed 720x1280.
    assert (analysis.width, analysis.height) == (720, 1280)

    result = await service().extract(
        source, workspace_in(tmp_path), analysis, Position.MIDDLE, "png"
    )

    written = probe(result.path)
    assert (written["width"], written["height"]) == (720, 1280)
    assert written["rotation"] == 0, "the still must not need rotating again"


@needs_ffmpeg
@pytest.mark.parametrize("position", list(Position))
async def test_every_position_produces_a_frame(position, videos, tmp_path):
    source = videos("landscape")
    analysis = await service().analyze(source)
    result = await service().extract(
        source, workspace_in(tmp_path), analysis, position, "jpeg",
        custom=3.5 if position is Position.CUSTOM else None,
    )
    assert result.size_bytes > 0
    assert 0 <= result.seconds <= analysis.duration


@needs_ffmpeg
async def test_a_still_image_is_not_a_video(tmp_path):
    photo = tmp_path / "still.png"
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x240", "-frames:v", "1", str(photo))
    with pytest.raises(MediaProcessingError) as error:
        await service().analyze(photo)
    assert error.value.code is ProcessingErrorCode.UNSUPPORTED


@needs_ffmpeg
async def test_the_frame_is_planned_without_scaling(videos, tmp_path):
    from app.services.media.frame import build_frame_args

    analysis = await service().analyze(videos("landscape"))
    plan = plan_frame(analysis, Position.MIDDLE, "jpeg")
    args = build_frame_args("ffmpeg", Path("/in.mp4"), Path("/out.jpg"), analysis, plan)

    assert "-vf" not in args and "scale" not in " ".join(args)
    assert args[args.index("-frames:v") + 1] == "1"
    assert "-map_metadata" in args and args[args.index("-map_metadata") + 1] == "-1"
    assert args.index("-ss") < args.index("-i"), "seek before decoding, not after"


def test_the_output_name_is_built_from_the_users_filename():
    assert frame_filename("Holiday Clip.mp4", "jpeg") == "atreox_frame_Holiday_Clip.jpg"
    assert frame_filename("clip.mov", "png") == "atreox_frame_clip.png"
    assert frame_filename(None, "jpeg") == "atreox_frame_video.jpg"
    assert frame_filename("../../etc/passwd", "png") == "atreox_frame_passwd.png"


# --- the flow, through the router ---------------------------------------------------


@pytest.fixture(autouse=True)
def treat_fakes_as_messages(monkeypatch):
    monkeypatch.setattr(frame_router, "Message", FakeMessage)


@pytest.fixture
def jobs(monkeypatch):
    recorder = FakeJobs()
    monkeypatch.setattr(frame_router, "JobsRepository", lambda session: recorder)
    return recorder


@pytest.fixture
def file_server(monkeypatch):
    server = FakeFileServer()
    monkeypatch.setattr(telegram_files, "AiohttpFileClient", lambda: server)
    return server


class FrameMessage(FakeMessage):
    async def edit_text(self, text=None, **kwargs):
        self.edits.append(text)
        self.markups.append(kwargs.get("reply_markup"))
        return self


def as_video(path: Path, *, size: int | None = None) -> FrameMessage:
    message = FrameMessage(keep=None)
    message.document = SimpleNamespace(
        file_id="VID-1", file_size=size or path.stat().st_size,
        file_name=path.name, mime_type="video/mp4",
    )
    return message


@needs_ffmpeg
async def test_the_whole_flow_from_menu_to_a_still(videos, tmp_path, clean_env, jobs,
                                                   file_server, session):
    source = videos("landscape")
    file_server.source = source
    settings = local_settings(tmp_path)
    context = fsm_for()
    message = as_video(source)
    message.keep = tmp_path / "received"
    bot = FakeBot(SERVER_FILE, size=source.stat().st_size)

    await frame_router.open_frame(FakeCallback(message), context, session=session)
    assert message.edits[-1] == texts.FRAME_PROMPT

    await frame_router.handle_media(
        message, context, bot=bot, settings=settings, session=session
    )
    assert await context.get_state() == FrameStates.choosing_position.state
    labels = [b.text for row in message.markups[-1].inline_keyboard for b in row]
    assert labels == [
        texts.BTN_FRAME_FIRST, texts.BTN_FRAME_QUARTER, texts.BTN_FRAME_MIDDLE,
        texts.BTN_FRAME_THREE_QUARTER, texts.BTN_FRAME_CUSTOM, texts.BTN_CANCEL,
    ]

    await frame_router.handle_position(
        FakeCallback(message), FrameCallback(action="pos", value="middle"), context
    )
    assert await context.get_state() == FrameStates.choosing_format.state
    assert message.edits[-1] == texts.FRAME_FORMAT_PROMPT

    await frame_router.handle_format(
        FakeCallback(message), FrameCallback(action="format", value="png"),
        context, bot=bot, settings=settings, session=session,
    )

    (document,) = message.documents
    assert document["filename"] == "atreox_frame_clip.png"
    assert document["no_detection"] is True
    assert document["probe"]["video_codec"] == "png"
    assert (document["probe"]["width"], document["probe"]["height"]) == (1280, 720)
    assert "Frame extracted" in message.answers[-1]
    assert jobs.created[0]["job_type"] is JobType.EXTRACT_FRAME
    assert jobs.status == "success"
    assert await context.get_state() is None
    assert workspaces_left(settings) == []


@needs_ffmpeg
async def test_a_custom_time_is_checked_against_the_videos_length(videos, tmp_path, clean_env,
                                                                  jobs, file_server, session):
    source = videos("landscape")                      # eight seconds
    file_server.source = source
    settings = local_settings(tmp_path)
    context = fsm_for()
    message = as_video(source)
    bot = FakeBot(SERVER_FILE, size=source.stat().st_size)

    await frame_router.handle_media(
        message, context, bot=bot, settings=settings, session=session
    )
    await frame_router.handle_position(
        FakeCallback(message), FrameCallback(action="pos", value="custom"), context
    )
    assert await context.get_state() == FrameStates.typing_time.state

    await frame_router.handle_time(
        FrameMessage(text="1:30"), context, bot=bot, settings=settings, session=session
    )
    assert await context.get_state() == FrameStates.typing_time.state, "out of range"

    await frame_router.handle_time(
        FrameMessage(text="0:05"), context, bot=bot, settings=settings, session=session
    )
    assert await context.get_state() == FrameStates.choosing_format.state

    await frame_router.handle_format(
        FakeCallback(message), FrameCallback(action="format", value="jpeg"),
        context, bot=bot, settings=settings, session=session,
    )
    (document,) = message.documents
    assert document["filename"].endswith(".jpg")
    assert "0:05" in message.answers[-1] or "00:05" in message.answers[-1]
    assert jobs.status == "success"


@needs_ffmpeg
async def test_a_large_video_takes_the_local_bot_api_path(videos, tmp_path, clean_env, jobs,
                                                          file_server, session):
    source = videos("landscape")
    file_server.source = source
    settings = local_settings(tmp_path)
    context = fsm_for()
    message = as_video(source, size=800 * MB)
    bot = FakeBot(SERVER_FILE, size=source.stat().st_size)

    await frame_router.handle_media(
        message, context, bot=bot, settings=settings, session=session
    )
    await frame_router.handle_position(
        FakeCallback(message), FrameCallback(action="pos", value="first"), context
    )
    await frame_router.handle_format(
        FakeCallback(message), FrameCallback(action="format", value="jpeg"),
        context, bot=bot, settings=settings, session=session,
    )

    assert jobs.status == "success", message.answers
    assert bot.downloads == []          # never the cloud download path
    assert file_server.requested and all(
        url.startswith("http://telegram-bot-api.railway.internal:8082/")
        for url in file_server.requested
    )
    assert workspaces_left(settings) == []


async def test_a_photo_is_not_accepted_as_a_video(tmp_path, clean_env, session):
    settings = local_settings(tmp_path)
    context = fsm_for()
    message = FrameMessage()
    message.photo = [SimpleNamespace(file_id="P", file_size=1000)]

    await frame_router.handle_media(
        message, context, bot=FakeBot(SERVER_FILE), settings=settings, session=session
    )

    assert message.answers[-1] == texts.FRAME_UNSUPPORTED
    assert message.documents == []


@needs_ffmpeg
async def test_a_failed_extraction_cleans_up_and_keeps_the_choices(videos, tmp_path, clean_env,
                                                                   jobs, file_server, session,
                                                                   monkeypatch):
    source = videos("landscape")
    file_server.source = source
    settings = local_settings(tmp_path)
    context = fsm_for()
    message = as_video(source)
    bot = FakeBot(SERVER_FILE, size=source.stat().st_size)

    await frame_router.handle_media(
        message, context, bot=bot, settings=settings, session=session
    )
    await frame_router.handle_position(
        FakeCallback(message), FrameCallback(action="pos", value="middle"), context
    )

    async def broken(*args, **kwargs):
        raise MediaProcessingError(ProcessingErrorCode.EMPTY_OUTPUT, "no frame there")

    monkeypatch.setattr(FrameService, "extract", broken)
    await frame_router.handle_format(
        FakeCallback(message), FrameCallback(action="format", value="jpeg"),
        context, bot=bot, settings=settings, session=session,
    )

    assert message.answers[-1] == texts.FRAME_NOT_FOUND
    assert jobs.status == "failed"
    assert message.documents == []
    assert await context.get_state() == FrameStates.choosing_format.state
    assert workspaces_left(settings) == []
