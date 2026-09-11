"""Regression: Media Optimizer on files above 20 MB.

Production symptom: files over ~20 MB answered "Something went wrong". The
logs showed the download was fine - 43 MB and 1.5 GB files arrived through the
local Bot API file endpoint - and every failure was ``ffmpeg failed rc=-9``
with nothing on stderr, a second into the encode. The container has 1 GB; left
to size its own thread pools from the host's CPUs, FFmpeg took a 2160x3840
60 fps source past 1-1.7 GB and the kernel killed it. Large files are simply
where 4K lives.

So these tests run the whole Telegram flow on real >20 MB media - the same
acquisition path as production (getFile answers an absolute local Bot API path,
streamed from the private file endpoint) - and measure FFmpeg's actual peak
memory, instead of only the optimizer service in isolation.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.bot import texts
from app.bot.callbacks import OptimizeCallback
from app.bot.routers import circle as circle_router
from app.bot.routers import optimizer as optimizer_router
from app.bot.states import OptimizerStates
from app.services import telegram_files
from app.services.media import image_worker
from app.services.media import optimizer as optimizer_media
from app.services.media.base import MediaProcessingError, ProcessingErrorCode
from app.services.telegram_files import FileKind, IncomingFile
from tests import test_large_files as large
from tests.test_large_files import (
    MB,
    SERVER_DIR,
    SERVER_FILE,
    FakeBot,
    FakeFileServer,
    FakeJobs,
    local_settings,
    workspaces_left,
)
from tests.test_media_optimizer import FakeCallback, FakeMessage, fsm_for, probe

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)

# What one optimizer encode may use. The bot's container has 1 GB, the bot
# itself ~200 MB, and a second heavy job may run alongside.
ENCODE_MEMORY_BUDGET_MB = 500
FILES_ENDPOINT = "http://telegram-bot-api.railway.internal:8082/"


# --- fixtures ---------------------------------------------------------------


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
                   check=True, timeout=900)


@pytest.fixture(scope="module")
def big_media(tmp_path_factory):
    root = tmp_path_factory.mktemp("large-media")
    cache: dict[str, Path] = {}

    def get(name: str) -> Path:
        if name in cache:
            return cache[name]
        if name == "4k60_25mb":
            # The production profile: a phone's portrait 4K at 60 fps, ~30 Mbps.
            path = root / "IMG_4K60.MOV"
            _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=2160x3840:rate=60",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "7",
                    "-vf", "noise=alls=12:allf=t", "-c:v", "libx264", "-preset", "ultrafast",
                    "-b:v", "30M", "-maxrate", "30M", "-bufsize", "60M", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-f", "mov", str(path))
        elif name == "1080p_100mb":
            path = root / "long_take.mp4"
            _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000", "-t", "15",
                    "-vf", "noise=alls=22:allf=t", "-c:v", "libx264", "-preset", "ultrafast",
                    "-b:v", "60M", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path))
        elif name == "jpeg_30mb":
            # 40 MP at quality 95: a real flagship-camera JPEG, well over 20 MB.
            path = root / "DSC_1000.JPG"
            grey = Image.effect_noise((7300, 5480), 40)
            Image.merge("RGB", (grey, grey.transpose(Image.Transpose.FLIP_LEFT_RIGHT), grey)).save(
                path, quality=95)
        else:
            raise AssertionError(name)
        cache[name] = path
        return path

    return get


@pytest.fixture(autouse=True)
def treat_fakes_as_messages(monkeypatch):
    monkeypatch.setattr(optimizer_router, "Message", FakeMessage)


@pytest.fixture
def jobs(monkeypatch):
    recorder = FakeJobs()
    monkeypatch.setattr(optimizer_router, "JobsRepository", lambda session: recorder)
    return recorder


@pytest.fixture
def file_server(monkeypatch):
    """The nginx endpoint next to the local Bot API, streaming 1 MB chunks."""
    server = FakeFileServer()
    monkeypatch.setattr(telegram_files, "AiohttpFileClient", lambda: server)
    return server


@pytest.fixture
def ffmpeg_peaks(monkeypatch):
    """FFmpeg's own peak RSS (``-benchmark``) for every optimizer encode, in MB."""
    peaks: list[float] = []
    real = optimizer_media.run_command_streaming

    async def measured(args, **kwargs):
        args = list(args)
        args.insert(1, "-benchmark")
        args[args.index("-loglevel") + 1] = "info"   # the bench line is info-level
        result = await real(args, **kwargs)
        match = re.search(r"maxrss=(\d+)", result.stderr)
        assert match, "ffmpeg reported no peak memory"
        peaks.append(int(match.group(1)) / 1024)
        return result

    monkeypatch.setattr(optimizer_media, "run_command_streaming", measured)
    return peaks


def as_video(path: Path) -> FakeMessage:
    """A file sent as a normal Telegram video (compressed by the client)."""
    return FakeMessage(video=SimpleNamespace(
        file_id="VIDEO-1", file_size=path.stat().st_size, file_name=None,
        mime_type="video/mp4", duration=7))


def as_document(path: Path, mime: str) -> FakeMessage:
    """The same bytes sent as a File."""
    return FakeMessage(document=SimpleNamespace(
        file_id="DOC-1", file_size=path.stat().st_size, file_name=path.name, mime_type=mime))


async def run_telegram_flow(tmp_path, source: Path, message: FakeMessage, preset: str,
                            file_server: FakeFileServer):
    """Send the file, read the summary, tap a preset - exactly as a user does."""
    file_server.source = source
    settings = local_settings(tmp_path)
    bot = FakeBot(SERVER_FILE, size=source.stat().st_size)
    context = fsm_for()
    await context.set_state(OptimizerStates.waiting_for_media)
    message.keep = tmp_path / "received"

    await optimizer_router.handle_media(message, context, bot=bot, settings=settings, session=None)
    summary = message.answers[-1]
    assert await context.get_state() == OptimizerStates.choosing_preset.state, summary

    await optimizer_router.handle_preset(
        FakeCallback(message), OptimizeCallback(action="preset", value=preset), context,
        bot=bot, settings=settings, session=None,
    )
    return settings, bot, context, summary


def assert_local_bot_api_path(bot: FakeBot, server: FakeFileServer, fetches: int) -> None:
    # Every byte came from the local Bot API's own file endpoint...
    assert len(server.requested) == fetches
    assert all(url.startswith(FILES_ENDPOINT) for url in server.requested)
    # ...never through the cloud download path (api.telegram.org).
    assert bot.downloads == []


# --- the production failure, reproduced -----------------------------------------


@needs_ffmpeg
async def test_25mb_4k60_video_sent_as_telegram_video_stays_within_memory(
    big_media, tmp_path, clean_env, jobs, file_server, ffmpeg_peaks
):
    source = big_media("4k60_25mb")
    assert source.stat().st_size > 25 * MB
    message = as_video(source)

    settings, bot, context, summary = await run_telegram_flow(
        tmp_path, source, message, "high", file_server)

    assert summary.splitlines()[1].startswith("2160×3840 • 00:07 • ")
    assert jobs.status == "success", message.answers
    assert jobs.error_code is None
    # The encode that production's kernel killed now fits the budget.
    assert ffmpeg_peaks and max(ffmpeg_peaks) < ENCODE_MEMORY_BUDGET_MB, ffmpeg_peaks
    (document,) = message.documents
    after = document["probe"]
    assert (after["width"], after["height"]) == (1080, 1920)
    assert after["video_codec"] == "h264" and after["audio_codecs"] == ["aac"]
    assert after["fps"] == pytest.approx(60, abs=0.5)
    assert document["size"] < source.stat().st_size
    assert message.answers[-1] == texts.optimize_done(source.stat().st_size, document["size"])
    assert_local_bot_api_path(bot, file_server, fetches=2)   # analysis + job
    assert file_server.deleted                                # server copy released
    assert workspaces_left(settings) == []
    assert await context.get_state() is None


@needs_ffmpeg
async def test_the_same_video_sent_as_a_file(big_media, tmp_path, clean_env, jobs, file_server,
                                             ffmpeg_peaks):
    source = big_media("4k60_25mb")
    message = as_document(source, "video/quicktime")

    settings, bot, _, _ = await run_telegram_flow(tmp_path, source, message, "balanced", file_server)

    assert jobs.status == "success", message.answers
    assert max(ffmpeg_peaks) < ENCODE_MEMORY_BUDGET_MB, ffmpeg_peaks
    (document,) = message.documents
    assert document["filename"] == "atreox_optimized_IMG_4K60.mp4"
    assert (document["probe"]["width"], document["probe"]["height"]) == (1080, 1920)
    assert_local_bot_api_path(bot, file_server, fetches=2)
    assert workspaces_left(settings) == []


@needs_ffmpeg
async def test_a_100mb_video_file(big_media, tmp_path, clean_env, jobs, file_server, ffmpeg_peaks):
    source = big_media("1080p_100mb")
    assert source.stat().st_size > 100 * MB
    message = as_document(source, "video/mp4")

    settings, bot, _, _ = await run_telegram_flow(tmp_path, source, message, "small", file_server)

    assert jobs.status == "success", message.answers
    assert max(ffmpeg_peaks) < ENCODE_MEMORY_BUDGET_MB, ffmpeg_peaks
    (document,) = message.documents
    after = document["probe"]
    assert (after["width"], after["height"]) == (1280, 720)
    assert after["duration"] == pytest.approx(15, abs=0.3)
    assert document["size"] < source.stat().st_size / 5
    assert_local_bot_api_path(bot, file_server, fetches=2)
    assert workspaces_left(settings) == []


@needs_ffmpeg
async def test_a_photo_file_over_20mb(big_media, tmp_path, clean_env, jobs, file_server):
    source = big_media("jpeg_30mb")
    assert source.stat().st_size > 20 * MB
    message = as_document(source, "image/jpeg")

    settings, bot, _, summary = await run_telegram_flow(
        tmp_path, source, message, "balanced", file_server)

    assert summary.startswith("🖼 <b>Photo detected</b>\n7300×5480 • JPEG • ")
    assert jobs.status == "success", message.answers
    (document,) = message.documents
    with Image.open(document["copy"]) as optimized:
        assert optimized.size == (7300, 5480) and optimized.format == "JPEG"
        # Above the buffered-JPEG size the worker streams a baseline JPEG.
        assert not optimized.info.get("progressive")
    assert document["size"] < source.stat().st_size
    assert_local_bot_api_path(bot, file_server, fetches=2)
    assert workspaces_left(settings) == []


async def test_no_20mb_limit_remains_on_the_way_in(tmp_path, clean_env, jobs, file_server):
    """A 1.5 GB declaration is admitted and fetched from the local endpoint."""
    folder = tmp_path / "tiny"
    folder.mkdir()
    source = folder / "clip.mp4"
    source.write_bytes(b"not really a video")          # analysis fails later, harmlessly
    file_server.source = source
    settings = local_settings(tmp_path)
    assert settings.input_limit_bytes == 2000 * MB
    message = FakeMessage(document=SimpleNamespace(
        file_id="HUGE", file_size=1500 * MB, file_name="clip.mp4", mime_type="video/mp4"))
    bot = FakeBot(SERVER_FILE, size=1500 * MB)
    context = fsm_for()
    await context.set_state(OptimizerStates.waiting_for_media)

    await optimizer_router.handle_media(message, context, bot=bot, settings=settings, session=None)

    assert texts.ERROR_TOO_LARGE not in message.answers
    assert_local_bot_api_path(bot, file_server, fetches=1)


# --- the fix, unit by unit ------------------------------------------------------


def test_threads_are_explicit_whatever_the_host_reports():
    from app.services.media.optimizer import MAX_ENCODE_THREADS, encode_threads

    assert encode_threads(2160, 3840, cpus=64) == MAX_ENCODE_THREADS == 2
    assert encode_threads(1920, 1080, cpus=1) == 1
    assert encode_threads(7680, 4320, cpus=64) == 1        # 8K: one decode thread


def test_encode_arguments_bound_every_thread_pool_and_the_lookahead():
    from app.services.media.optimizer import RC_LOOKAHEAD, plan_video

    source = large_analysis()
    plan = plan_video(source, optimizer_media.Preset.HIGH, cpus=64)
    args = optimizer_media.build_video_args("ffmpeg", Path("/in.mov"), Path("/out.mp4"), source, plan)
    input_at = args.index("-i")
    decoder = [i for i, a in enumerate(args) if a == "-threads" and i < input_at]
    encoder = [i for i, a in enumerate(args) if a == "-threads" and i > input_at]
    assert decoder and args[decoder[0] + 1] == "2"
    assert encoder and args[encoder[0] + 1] == "2"
    assert args[args.index("-filter_threads") + 1] == "2"
    assert args[args.index("-rc-lookahead") + 1] == str(RC_LOOKAHEAD) == "10"


def large_analysis(**overrides):
    values = dict(size_bytes=43 * MB, width=2160, height=3840, duration=11.5, video_codec="h264",
                  video_stream=0, frame_rate=60.0, video_bitrate=30_000_000, bitrate_declared=True,
                  audio_codec="aac", audio_stream=1, audio_bitrate=192_000, audio_channels=2)
    values.update(overrides)
    return optimizer_media.VideoAnalysis(**values)


def test_a_killed_encode_is_named_for_what_it_is():
    code = optimizer_media.classify_failure("", returncode=-9)
    assert code is ProcessingErrorCode.PROCESS_KILLED
    error = MediaProcessingError(code, "ffmpeg exited with code -9")
    assert optimizer_router._failure_text(error) == texts.OPTIMIZE_OUT_OF_MEMORY
    assert optimizer_router._failure_text(error) != texts.ERROR_PROCESSING


def test_photo_budgets_match_between_service_and_worker():
    from app.services.media.optimizer import IMAGE_PIXEL_LIMITS

    assert image_worker.MAX_PIXELS == IMAGE_PIXEL_LIMITS


@needs_ffmpeg
@pytest.mark.parametrize(
    "name,size,fmt",
    [("huge.jpg", (9000, 6000), "JPEG"),       # 54 MP > 50 MP
     ("huge.webp", (4200, 4000), "WEBP")],     # 16.8 MP > 16 MP
)
async def test_photos_over_the_pixel_budget_are_refused_before_any_decode(
    name, size, fmt, tmp_path, clean_env, jobs
):
    folder = tmp_path / "server"
    folder.mkdir()
    path = folder / name
    Image.new("L" if fmt == "JPEG" else "RGB", size, 128).save(path, fmt)

    context = fsm_for()
    message = FakeMessage()
    await optimizer_router._analyze(
        message=message, state=context, bot=FakeBot(str(path), size=path.stat().st_size),
        settings=local_settings(tmp_path, bot_api_local_dir=str(folder)), session=None,
        incoming=IncomingFile(file_id="P", kind=FileKind.IMAGE, size=path.stat().st_size,
                              original_filename=name),
        user_id=42,
    )
    assert message.answers[-1] == texts.ERROR_TOO_LARGE
    assert jobs.error_code == "file_too_large"


def test_the_worker_enforces_the_budget_itself(tmp_path):
    """Pillow only raises at twice MAX_IMAGE_PIXELS; the worker must not rely on it."""
    path = tmp_path / "wide.png"
    Image.new("L", (10_000, 5_100), 0).save(path)          # 51 MP
    assert image_worker.main(["w", str(path), str(tmp_path / "out.png"), "png", "small"]) \
        == image_worker.EXIT_TOO_LARGE
    assert not (tmp_path / "out.png").exists()


# --- Circle's large-file flow is unaffected ------------------------------------------


@needs_ffmpeg
async def test_circle_still_handles_the_same_large_file(big_media, tmp_path, clean_env,
                                                        monkeypatch, file_server):
    source = big_media("4k60_25mb")
    file_server.source = source
    monkeypatch.setattr(circle_router, "Message", large.FakeMessage)
    recorder = FakeJobs()
    monkeypatch.setattr(circle_router, "JobsRepository", lambda session: recorder)
    settings = local_settings(tmp_path)
    message = large.FakeMessage(probe_circles=True)

    outcome = await circle_router._run_circle_job(
        message=message, state=fsm_for(), bot=FakeBot(SERVER_FILE, size=source.stat().st_size),
        settings=settings, session=None,
        incoming=IncomingFile(file_id="C", kind=FileKind.VIDEO, size=source.stat().st_size,
                              mime_type="video/quicktime"),
        mode=circle_router.CircleMode.AUTO, user_id=42,
    )

    assert outcome is circle_router._Outcome.DONE, message.answers
    assert recorder.status == "success"
    (circle,) = message.circles
    assert circle["dimensions"] == (128, 128) and circle["has_audio"]
    assert circle["duration"] == pytest.approx(7, abs=0.2)
    assert file_server.requested and file_server.deleted
    assert workspaces_left(settings) == []


def test_the_server_directory_is_the_one_production_uses():
    assert SERVER_FILE.startswith(SERVER_DIR)
