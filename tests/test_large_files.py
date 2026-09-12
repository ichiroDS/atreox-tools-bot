"""Phase 6: large files, the local Bot API, long videos and resource safety.

Telegram and the database are faked; ffmpeg is real where a test says so
(and those tests skip on a machine without it).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tracemalloc
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot import texts
from app.bot.callbacks import CircleCallback
from app.bot.keyboards.circle import long_video_choices
from app.bot.routers import circle as circle_router
from app.bot.routers import metadata as metadata_router
from app.bot.states import CircleStates, ChangeMetadataStates, CleanMetadataStates
from app.config import Settings
from app.main import create_bot
from app.services import telegram_files
from app.services.jobgate import BusyReason, MediaJobGate, ServerBusy
from app.services.media.base import MediaInfo, MediaProcessingError, ProcessingErrorCode
from app.services.media.circle import (
    CircleResult,
    build_circle_ffmpeg_args,
    needs_split,
    plan_circle_segments,
)
from app.services.media.probe import parse_probe_output
from app.services.telegram_files import (
    FileKind,
    FileTooLargeError,
    IncomingFile,
    OutputTooLargeError,
    SourceKind,
    TelegramFileService,
    ensure_sendable,
    resolve_file_location,
    stream_to_file,
    transfer_timeout,
)
from app.utils.temp_files import JobWorkspace, sweep_stale_workspaces

MB = 1024 * 1024
TOKEN = "123456789:AAFakeTokenForTestsOnly-0123456789abcdef"
SERVER_DIR = "/var/lib/telegram-bot-api"
SERVER_FILE = f"{SERVER_DIR}/{TOKEN}/videos/file_7.mp4"

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


# --- fakes ------------------------------------------------------------------


class FakeMessage:
    """Records what a handler sent, including every circle."""

    def __init__(self, user_id: int = 42, *, probe_circles: bool = False, parent=None):
        self.chat = SimpleNamespace(id=user_id)
        self.from_user = SimpleNamespace(id=user_id, is_bot=False)
        self.video = None
        self.document = None
        self.photo = None
        self.answers: list[str] = []
        self.markups: list = []
        self.edits: list[str] = []
        self.circles: list[dict] = []
        self._probe = probe_circles
        self._parent = parent

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)
        self.markups.append(kwargs.get("reply_markup"))
        return FakeMessage(parent=self)

    async def answer_video_note(self, video_note=None, **kwargs):
        # Inspect the file now: the job deletes it right after sending.
        path = Path(video_note.path)
        record = {"size": path.stat().st_size, "duration_arg": kwargs.get("duration")}
        if self._probe:
            info = ffprobe(path)
            record.update(duration=info.duration, has_audio=info.has_audio,
                          dimensions=(info.width, info.height))
        self.circles.append(record)
        return self

    async def edit_text(self, text=None, **kwargs):
        (self._parent or self).edits.append(text)
        return self

    async def delete(self):
        return True


class FakeCallback:
    def __init__(self, message: FakeMessage, user_id: int = 42):
        self.message = message
        self.from_user = SimpleNamespace(id=user_id, is_bot=False)
        self.answers: list = []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)


class FakeBot:
    """A Bot API server. ``file_path`` is whatever getFile should answer."""

    def __init__(self, file_path: str, *, size: int | None = None, cloud_source: Path | None = None):
        self.file_path = file_path
        self.size = size
        self.cloud_source = cloud_source
        self.downloads: list[dict] = []
        self.timeouts: list[int] = []
        self.get_file_timeouts: list[int | None] = []

    async def get_file(self, file_id, request_timeout=None):
        self.get_file_timeouts.append(request_timeout)
        return SimpleNamespace(file_path=self.file_path, file_size=self.size)

    async def download_file(self, file_path, destination=None, **kwargs):
        self.downloads.append({"file_path": file_path, "destination": destination, **kwargs})
        shutil.copyfile(self.cloud_source, destination)

    async def __call__(self, method, request_timeout=None):
        self.timeouts.append(request_timeout)
        return await method


class FakeFileServer:
    """Stands in for the nginx file endpoint next to the local Bot API."""

    def __init__(self, source: Path | None = None, *, chunks: int = 0, chunk_size: int = MB):
        self.source = source
        self.chunks = chunks
        self.chunk_size = chunk_size
        self.requested: list[str] = []
        self.deleted: list[str] = []

    def stream(self, url, *, chunk_size):
        self.requested.append(url)
        return self._stream(chunk_size)

    async def _stream(self, chunk_size):
        if self.source is not None:
            with open(self.source, "rb") as handle:
                while chunk := handle.read(chunk_size):
                    yield chunk
        else:
            for _ in range(self.chunks):
                # A fresh allocation per chunk, as a socket read would be.
                yield bytes(self.chunk_size)

    async def delete(self, url):
        self.deleted.append(url)


class FakeJobs:
    def __init__(self):
        self.created: list[dict] = []
        self.status = None
        self.error_code = None
        self.output_size = None

    async def create(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(id=kwargs["job_id"])

    async def mark_success(self, job, *, output_size=None):
        self.status, self.output_size = "success", output_size

    async def mark_failed(self, job, *, error_code):
        self.status, self.error_code = "failed", error_code


class FakeCircleService:
    """Plans like the real service; encodes instantly, optionally failing."""

    def __init__(self, duration: float, *, has_audio=True, fail_at=None, output_bytes=2048):
        self.duration = duration
        self.has_audio = has_audio
        self.fail_at = fail_at
        self.output_bytes = output_bytes
        self.encoded: list = []
        self.sources: list[Path] = []

    async def probe(self, source, *, job_id=None):
        assert Path(source).exists()
        self.sources.append(Path(source))
        return MediaInfo(width=640, height=360, duration=self.duration,
                         audio_codec="aac" if self.has_audio else None)

    def plan(self, info):
        return plan_circle_segments(info.duration, 60)

    async def encode_segment(self, source, destination, segment, *, with_audio, job_id=None):
        self.encoded.append(segment)
        if segment.index == self.fail_at:
            raise MediaProcessingError(ProcessingErrorCode.ENCODE_FAILED, "boom")
        destination.write_bytes(b"\0" * self.output_bytes)
        return CircleResult(path=destination, size_bytes=self.output_bytes, filename=destination.name)


def ffprobe(path: Path) -> MediaInfo:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
         "-show_streams", "--", str(path)],
        capture_output=True, text=True, check=True,
    )
    return parse_probe_output(result.stdout)


def make_video_sync(path: Path, *, seconds: float, audio: bool = True, size="160x120") -> Path:
    args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc=size={size}:rate=10"]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440"]
    args += ["-t", str(seconds), "-pix_fmt", "yuv420p", "-c:v", "libx264",
             "-preset", "ultrafast"]
    args += ["-c:a", "aac"] if audio else ["-an"]
    args.append(str(path))
    subprocess.run(args, check=True, timeout=300)
    return path


@pytest.fixture(scope="module")
def video_factory(tmp_path_factory):
    """Real test videos, generated once per module and shared read-only."""
    cache: dict = {}
    root = tmp_path_factory.mktemp("videos")

    def get(seconds: float, *, audio: bool = True) -> Path:
        key = (seconds, audio)
        if key not in cache:
            name = f"v{str(seconds).replace('.', '_')}_{'a' if audio else 's'}.mp4"
            cache[key] = make_video_sync(root / name, seconds=seconds, audio=audio)
        return cache[key]

    return get


@pytest_asyncio.fixture
async def state():
    context = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=42))
    yield context
    await context.clear()


def fsm_for(user_id: int) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    )


@pytest.fixture
def jobs(monkeypatch):
    recorder = FakeJobs()
    monkeypatch.setattr(circle_router, "JobsRepository", lambda session: recorder)
    monkeypatch.setattr(metadata_router, "JobsRepository", lambda session: recorder)
    return recorder


@pytest.fixture(autouse=True)
def treat_fakes_as_messages(monkeypatch):
    monkeypatch.setattr(circle_router, "Message", FakeMessage)
    monkeypatch.setattr(metadata_router, "Message", FakeMessage)


@pytest.fixture
def file_server(monkeypatch):
    server = FakeFileServer()
    monkeypatch.setattr(telegram_files, "AiohttpFileClient", lambda: server)
    return server


def local_settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        bot_token=TOKEN,
        bot_api_base_url="http://telegram-bot-api.railway.internal:8081",
        bot_api_files_url="http://telegram-bot-api.railway.internal:8082",
        bot_api_local_dir=SERVER_DIR,
        temp_root=tmp_path / "work",
        video_note_size=128,
        process_timeout_seconds=300,
    )
    values.update(overrides)
    return Settings(**values)


def workspace_in(tmp_path: Path) -> JobWorkspace:
    path = tmp_path / f"ws-{uuid.uuid4().hex[:6]}"
    path.mkdir()
    return JobWorkspace(job_id=uuid.uuid4(), path=path)


def video(*, size=10 * MB, duration=None, file_id="VID-1", name=None):
    return IncomingFile(file_id=file_id, kind=FileKind.VIDEO, size=size,
                        original_filename=name, mime_type="video/mp4", duration=duration)


def workspaces_left(settings: Settings) -> list:
    root = Path(settings.temp_root)
    return list(root.iterdir()) if root.exists() else []


async def run_job(settings, *, bot, message, state, incoming, mode, gate=None, user_id=42):
    return await circle_router._run_circle_job(
        message=message, state=state, bot=bot, settings=settings, session=None,
        incoming=incoming, mode=mode, user_id=user_id, user=None, media_gate=gate,
    )


# --- getFile: official URL vs local absolute path ---------------------------


def test_official_bot_api_builds_the_standard_file_url(clean_env):
    bot = create_bot(Settings(bot_token=TOKEN))
    assert bot.session.api.is_local is False
    assert (
        bot.session.api.file_url(TOKEN, "videos/file_1.mp4")
        == f"https://api.telegram.org/file/bot{TOKEN}/videos/file_1.mp4"
    )


def test_local_bot_api_is_marked_local_and_uses_the_custom_base(clean_env):
    bot = create_bot(Settings(bot_token=TOKEN, bot_api_base_url="http://bot-api:8081"))
    assert bot.session.api.is_local is True
    assert bot.session.api.api_url(TOKEN, "getMe") == f"http://bot-api:8081/bot{TOKEN}/getMe"


def test_a_relative_path_is_an_official_download():
    location = resolve_file_location(
        "videos/file_1.mp4", local_mode=False, local_dir=SERVER_DIR, files_base_url=None
    )
    assert location.kind is SourceKind.CLOUD
    assert location.file_path == "videos/file_1.mp4"


async def test_official_get_file_streams_into_the_workspace(tmp_path):
    source = tmp_path / "cloud.mp4"
    source.write_bytes(b"v" * 4096)
    bot = FakeBot("videos/file_1.mp4", size=4096, cloud_source=source)
    service = TelegramFileService(bot, max_file_size_bytes=20 * MB)
    workspace = workspace_in(tmp_path)

    fetched = await service.fetch(video(size=4096), workspace)

    call = bot.downloads[0]
    assert call["file_path"] == "videos/file_1.mp4"
    # Always to a path on disk, never to an in-memory buffer.
    assert Path(call["destination"]).parent == workspace.path
    assert call["timeout"] >= 120
    assert fetched.location.kind is SourceKind.CLOUD
    assert fetched.borrowed is False
    assert fetched.path.read_bytes() == source.read_bytes()


def test_a_local_absolute_path_on_a_shared_disk_is_used_in_place(tmp_path):
    shared = tmp_path / "file_3.mp4"
    shared.write_bytes(b"x")
    location = resolve_file_location(
        str(shared), local_mode=True, local_dir=str(tmp_path), files_base_url=None
    )
    assert location.kind is SourceKind.LOCAL_PATH


async def test_a_borrowed_file_is_never_copied(tmp_path):
    shared = tmp_path / "server" / "file_3.mp4"
    shared.parent.mkdir()
    shared.write_bytes(b"x" * 1000)
    service = TelegramFileService(
        FakeBot(str(shared), size=1000), max_file_size_bytes=MB,
        local_mode=True, local_dir=str(tmp_path / "server"),
    )
    workspace = workspace_in(tmp_path)

    fetched = await service.fetch(video(size=1000), workspace)

    assert fetched.path == shared
    assert fetched.borrowed is True
    assert list(workspace.path.iterdir()) == []


def test_a_local_path_in_another_container_maps_to_the_file_server():
    location = resolve_file_location(
        SERVER_FILE,
        local_mode=True,
        local_dir=SERVER_DIR,
        files_base_url="http://telegram-bot-api.railway.internal:8082/",
        path_exists=lambda _: False,
    )
    assert location.kind is SourceKind.LOCAL_HTTP
    # The token directory's colon is escaped; the path layout is preserved.
    assert location.url == (
        "http://telegram-bot-api.railway.internal:8082/"
        "123456789%3AAAFakeTokenForTestsOnly-0123456789abcdef/videos/file_7.mp4"
    )


@pytest.mark.parametrize(
    "file_path",
    ["/etc/passwd", "/var/lib/telegram-bot-api-evil/x/videos/a.mp4",
     "/var/lib/telegram-bot-api/../../etc/shadow"],
)
def test_paths_outside_the_server_directory_are_refused(file_path):
    with pytest.raises(MediaProcessingError) as excinfo:
        resolve_file_location(file_path, local_mode=True, local_dir=SERVER_DIR,
                              files_base_url="http://files:8082", path_exists=lambda _: False)
    assert excinfo.value.code is ProcessingErrorCode.DOWNLOAD_FAILED


def test_an_unreachable_local_path_fails_clearly():
    with pytest.raises(MediaProcessingError) as excinfo:
        resolve_file_location(SERVER_FILE, local_mode=True, local_dir=SERVER_DIR,
                              files_base_url=None, path_exists=lambda _: False)
    assert "BOT_API_FILES_URL" in str(excinfo.value)


def test_an_absolute_path_from_the_cloud_api_is_refused():
    with pytest.raises(MediaProcessingError):
        resolve_file_location(SERVER_FILE, local_mode=False, local_dir=SERVER_DIR,
                              files_base_url=None, path_exists=lambda _: True)


async def test_a_file_server_download_lands_in_the_workspace_and_is_released(tmp_path):
    source = tmp_path / "remote.mp4"
    source.write_bytes(b"r" * (3 * MB + 17))
    server = FakeFileServer(source)
    service = TelegramFileService(
        FakeBot(SERVER_FILE, size=source.stat().st_size), max_file_size_bytes=10 * MB,
        local_mode=True, local_dir=SERVER_DIR, files_base_url="http://files:8082", http=server,
    )
    workspace = workspace_in(tmp_path)

    fetched = await service.fetch(video(size=None), workspace)
    assert fetched.location.kind is SourceKind.LOCAL_HTTP
    assert fetched.path.parent == workspace.path
    assert fetched.path.read_bytes() == source.read_bytes()
    assert server.requested == [fetched.location.url]

    await service.release(fetched)
    assert server.deleted == [fetched.location.url]


# --- size limits ------------------------------------------------------------


def test_the_too_large_copy_is_exactly_as_specified():
    assert texts.ERROR_TOO_LARGE == "⚠️ This file is too large for the current service limit."


async def test_a_stream_that_outgrows_the_limit_is_aborted_and_removed(tmp_path):
    server = FakeFileServer(chunks=5)
    service = TelegramFileService(
        FakeBot(SERVER_FILE, size=None), max_file_size_bytes=3 * MB,
        local_mode=True, local_dir=SERVER_DIR, files_base_url="http://files:8082", http=server,
    )
    workspace = workspace_in(tmp_path)
    with pytest.raises(FileTooLargeError):
        await service.fetch(video(size=None), workspace)
    # The partial download does not linger in the workspace.
    assert list(workspace.path.iterdir()) == []


async def test_a_file_above_the_old_20mb_cap_is_accepted_in_local_mode(tmp_path, clean_env):
    settings = local_settings(tmp_path)
    service = TelegramFileService.from_settings(FakeBot(SERVER_FILE), settings)
    service.check_size(video(size=1500 * MB))  # would have been refused before
    with pytest.raises(FileTooLargeError):
        service.check_size(video(size=2001 * MB))


async def test_the_cloud_getfile_too_big_error_maps_to_the_size_message(tmp_path):
    class RefusingBot(FakeBot):
        async def get_file(self, file_id, request_timeout=None):
            raise RuntimeError("Telegram server says - Bad Request: file is too big")

    service = TelegramFileService(RefusingBot("x"), max_file_size_bytes=20 * MB)
    with pytest.raises(FileTooLargeError):
        await service.fetch(video(size=None), workspace_in(tmp_path))


async def test_circle_handler_refuses_an_oversized_video_before_fetching(tmp_path, clean_env, state):
    settings = Settings(bot_token=TOKEN, temp_root=tmp_path / "work")  # cloud: 20 MB
    await state.set_state(CircleStates.waiting_for_video)
    message = FakeMessage()
    message.video = SimpleNamespace(file_id="BIG", file_size=25 * MB, file_name=None,
                                    mime_type="video/mp4", duration=10)
    bot = FakeBot("videos/x.mp4")

    await circle_router.handle_video(message, state, bot=bot, settings=settings, session=None)

    assert message.answers == [texts.ERROR_TOO_LARGE]
    assert bot.downloads == []
    # Still waiting, so a smaller file can follow.
    assert await state.get_state() == CircleStates.waiting_for_video.state


async def test_metadata_wizard_refuses_an_oversized_file_up_front(tmp_path, clean_env, state):
    settings = local_settings(tmp_path)
    await state.set_state(ChangeMetadataStates.waiting_for_file)
    message = FakeMessage()
    message.document = SimpleNamespace(file_id="D", file_size=1990 * MB,
                                       file_name="clip.mp4", mime_type="video/mp4")
    await metadata_router.handle_change_file(message, state, settings=settings)
    # 1990 MB fits the input limit but could never be sent back (1950 MB).
    assert message.answers == [texts.ERROR_TOO_LARGE]
    assert await state.get_state() == ChangeMetadataStates.waiting_for_file.state


async def test_metadata_accepts_a_photo_document_far_above_20mb(tmp_path, clean_env, state):
    settings = local_settings(tmp_path)
    await state.set_state(ChangeMetadataStates.waiting_for_file)
    message = FakeMessage()
    message.document = SimpleNamespace(file_id="D", file_size=180 * MB,
                                       file_name="IMG_0001.HEIC", mime_type="image/heic")
    await metadata_router.handle_change_file(message, state, settings=settings)
    assert await state.get_state() == ChangeMetadataStates.choosing_generation.state
    assert (await state.get_data())["file"]["size"] == 180 * MB


# --- memory -----------------------------------------------------------------


async def test_streaming_a_large_file_keeps_memory_flat(tmp_path):
    server = FakeFileServer(chunks=64)  # 64 MB
    service = TelegramFileService(
        FakeBot(SERVER_FILE, size=64 * MB), max_file_size_bytes=100 * MB,
        local_mode=True, local_dir=SERVER_DIR, files_base_url="http://files:8082", http=server,
    )
    tracemalloc.start()
    try:
        fetched = await service.fetch(video(size=64 * MB), workspace_in(tmp_path))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert fetched.size == 64 * MB
    # A whole-file read would peak at 64 MB or more; chunking stays near one chunk.
    assert peak < 8 * MB, f"peak {peak / MB:.1f} MB"


async def test_stream_to_file_holds_one_chunk_at_a_time(tmp_path):
    async def chunks():
        for _ in range(32):
            yield bytes(MB)

    tracemalloc.start()
    try:
        written = await stream_to_file(chunks(), tmp_path / "big.bin", limit_bytes=64 * MB)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert written == 32 * MB
    assert peak < 8 * MB


# --- output guard -----------------------------------------------------------


def test_output_guard_refuses_an_upload_that_would_fail():
    ensure_sendable(1950 * MB, 1950 * MB)
    with pytest.raises(OutputTooLargeError):
        ensure_sendable(1950 * MB + 1, 1950 * MB)


def test_upload_timeouts_grow_with_the_file():
    assert transfer_timeout(1 * MB) < transfer_timeout(500 * MB) < transfer_timeout(1950 * MB)
    assert transfer_timeout(None) >= 60
    assert transfer_timeout(100_000 * MB) == 7200


async def test_an_oversized_circle_is_not_sent(tmp_path, clean_env, state, jobs, monkeypatch, file_server):
    settings = local_settings(tmp_path, max_output_file_size_mb=1)
    monkeypatch.setattr(circle_router, "_circle_service",
                        lambda s: FakeCircleService(30, output_bytes=2 * MB))
    file_server.chunks = 1
    message = FakeMessage()

    await run_job(settings, bot=FakeBot(SERVER_FILE, size=MB), message=message, state=state,
                  incoming=video(size=MB), mode=circle_router.CircleMode.AUTO)

    assert message.circles == []
    assert texts.ERROR_OUTPUT_TOO_LARGE in message.answers
    assert jobs.error_code == ProcessingErrorCode.OUTPUT_TOO_LARGE.value
    assert workspaces_left(settings) == []


# --- split planning ---------------------------------------------------------


def spans(segments):
    return [(round(s.start, 3), round(s.duration, 3)) for s in segments]


def test_61_seconds_becomes_a_full_circle_and_a_one_second_tail():
    assert spans(plan_circle_segments(61, 60)) == [(0, 60), (60, 1)]


def test_two_and_a_half_minutes_become_three_circles():
    assert spans(plan_circle_segments(150, 60)) == [(0, 60), (60, 60), (120, 30)]


@pytest.mark.parametrize(
    "duration,count",
    [(59.9, 1), (60, 1), (60.4, 1), (60.6, 2), (120, 2), (120.3, 2), (121, 3), (3600, 60)],
)
def test_exact_boundaries_do_not_produce_a_sliver(duration, count):
    assert len(plan_circle_segments(duration, 60)) == count


@pytest.mark.parametrize("duration", [61, 150, 125.7, 599.99, 3601.5])
def test_segments_tile_the_timeline_without_overlap_or_gaps(duration):
    segments = plan_circle_segments(duration, 60)
    assert segments[0].start == 0
    for current, following in zip(segments, segments[1:]):
        assert current.end == pytest.approx(following.start)
        assert current.duration == 60
    assert all(0 < s.duration <= 60 for s in segments)
    # Everything up to the container-slop tolerance is covered.
    assert duration - segments[-1].end < 0.5 + 1e-9


def test_the_split_limit_never_exceeds_telegrams():
    assert spans(plan_circle_segments(150, 90)) == [(0, 60), (60, 60), (120, 30)]


def test_unknown_duration_is_a_single_capped_circle():
    assert spans(plan_circle_segments(0, 60)) == [(0, 60)]


@pytest.mark.parametrize(
    "duration,expected", [(None, False), (60, False), (60.5, False), (61, True), (150, True)]
)
def test_needs_split(duration, expected):
    assert needs_split(duration, 60) is expected


def test_a_segment_seeks_on_the_input_side():
    args = build_circle_ffmpeg_args(
        "ffmpeg", Path("/in.mp4"), Path("/out.mp4"), size=384, duration_limit=30, start=120
    )
    assert args[args.index("-ss") + 1] == "120.000"
    assert args.index("-ss") < args.index("-i")
    assert float(args[args.index("-t") + 1]) == 30.0


def test_the_first_segment_does_not_seek():
    args = build_circle_ffmpeg_args("ffmpeg", Path("/in.mp4"), Path("/out.mp4"),
                                     size=384, duration_limit=60)
    assert "-ss" not in args


# --- long video UX ----------------------------------------------------------


async def test_a_long_native_video_is_asked_about_before_any_download(tmp_path, clean_env, state):
    settings = local_settings(tmp_path)
    await state.set_state(CircleStates.waiting_for_video)
    message = FakeMessage()
    message.video = SimpleNamespace(file_id="LONG", file_size=40 * MB, file_name=None,
                                    mime_type="video/mp4", duration=61)
    bot = FakeBot(SERVER_FILE)

    await circle_router.handle_video(message, state, bot=bot, settings=settings, session=None)

    assert message.answers == ["🎥 This video is 1:01 long.\n\nWhat should I do?"]
    assert bot.downloads == []
    assert await state.get_state() == CircleStates.choosing_long_mode.state
    assert (await state.get_data())["circle_file"]["file_id"] == "LONG"
    labels = [b.text for row in message.markups[0].inline_keyboard for b in row]
    assert labels == ["✂️ Split into Circles", "▶️ First Circle Only", "❌ Cancel"]


def test_the_long_video_keyboard_fits_telegrams_budget():
    for row in long_video_choices().inline_keyboard:
        for button in row:
            assert len(button.callback_data.encode()) <= 64


@pytest.mark.parametrize("seconds,label", [(61, "1:01"), (150, "2:30"), (3723, "1:02:03")])
def test_durations_read_like_a_player(seconds, label):
    assert texts.format_duration(seconds) == label


async def test_a_video_at_exactly_the_limit_is_processed_without_asking(
    tmp_path, clean_env, state, jobs, monkeypatch, file_server
):
    settings = local_settings(tmp_path)
    service = FakeCircleService(60.0)
    monkeypatch.setattr(circle_router, "_circle_service", lambda s: service)
    file_server.chunks = 1
    await state.set_state(CircleStates.waiting_for_video)
    message = FakeMessage()
    message.video = SimpleNamespace(file_id="V", file_size=MB, file_name=None,
                                    mime_type="video/mp4", duration=60)

    await circle_router.handle_video(message, state, bot=FakeBot(SERVER_FILE, size=MB),
                                     settings=settings, session=None)

    assert len(message.circles) == 1
    assert texts.CIRCLE_DONE in message.answers
    assert jobs.status == "success"


async def test_a_long_file_of_unknown_length_is_asked_about_after_probing(
    tmp_path, clean_env, state, jobs, monkeypatch, file_server
):
    settings = local_settings(tmp_path)
    monkeypatch.setattr(circle_router, "_circle_service", lambda s: FakeCircleService(150))
    file_server.chunks = 2
    message = FakeMessage()

    outcome = await run_job(settings, bot=FakeBot(SERVER_FILE, size=2 * MB), message=message,
                            state=state, incoming=video(size=2 * MB),
                            mode=circle_router.CircleMode.AUTO)

    assert outcome is circle_router._Outcome.ASKED
    assert "2:30" in message.answers[-1]
    assert await state.get_state() == CircleStates.choosing_long_mode.state
    assert message.circles == []
    # No job was run, so none is recorded; the local copy is gone...
    assert jobs.created == []
    assert workspaces_left(settings) == []
    # ...but the Bot API server keeps its copy for the answer.
    assert file_server.deleted == []


async def test_first_circle_only_sends_exactly_one(tmp_path, clean_env, state, jobs, monkeypatch, file_server):
    settings = local_settings(tmp_path)
    service = FakeCircleService(150)
    monkeypatch.setattr(circle_router, "_circle_service", lambda s: service)
    file_server.chunks = 1
    await state.set_state(CircleStates.choosing_long_mode)
    await state.update_data(circle_file=video(size=MB, duration=150).to_dict())
    message = FakeMessage()

    await circle_router.handle_long_video_choice(
        FakeCallback(message), CircleCallback(action="first"), state,
        bot=FakeBot(SERVER_FILE, size=MB), settings=settings, session=None,
    )

    assert [s.index for s in service.encoded] == [0]
    assert len(message.circles) == 1
    assert jobs.status == "success"
    assert await state.get_state() is None


async def test_split_sends_every_circle_in_order_with_progress(
    tmp_path, clean_env, state, jobs, monkeypatch, file_server
):
    settings = local_settings(tmp_path)
    service = FakeCircleService(150)
    monkeypatch.setattr(circle_router, "_circle_service", lambda s: service)
    file_server.chunks = 1
    await state.set_state(CircleStates.choosing_long_mode)
    await state.update_data(circle_file=video(size=MB, duration=150).to_dict())
    message = FakeMessage()
    bot = FakeBot(SERVER_FILE, size=MB)

    await circle_router.handle_long_video_choice(
        FakeCallback(message), CircleCallback(action="split"), state,
        bot=bot, settings=settings, session=None,
    )

    assert [(s.start, s.duration) for s in service.encoded] == [(0, 60), (60, 60), (120, 30)]
    assert [c["duration_arg"] for c in message.circles] == [60, 60, 30]
    assert message.edits == [texts.circle_progress(i, 3) for i in (1, 2, 3)]
    assert jobs.status == "success" and jobs.output_size == 3 * 2048
    assert all(timeout and timeout >= 120 for timeout in bot.timeouts)
    assert file_server.deleted, "the Bot API server's copy was not released"
    assert workspaces_left(settings) == []


async def test_cancel_clears_the_choice(state):
    await state.set_state(CircleStates.choosing_long_mode)
    await state.update_data(circle_file=video().to_dict())
    callback = FakeCallback(FakeMessage())
    await circle_router.cancel_long_video(callback, state)
    assert await state.get_state() is None
    assert callback.answers == [texts.CANCELLED]


async def test_a_stale_choice_is_explained_not_executed():
    callback = FakeCallback(FakeMessage())
    await circle_router.stale_long_video_choice(callback)
    assert callback.answers == [texts.CIRCLE_CHOICE_EXPIRED]


async def test_cleanup_after_a_split_fails_half_way(
    tmp_path, clean_env, state, jobs, monkeypatch, file_server
):
    settings = local_settings(tmp_path)
    service = FakeCircleService(150, fail_at=1)
    monkeypatch.setattr(circle_router, "_circle_service", lambda s: service)
    file_server.chunks = 1
    message = FakeMessage()

    outcome = await run_job(settings, bot=FakeBot(SERVER_FILE, size=MB), message=message,
                            state=state, incoming=video(size=MB),
                            mode=circle_router.CircleMode.SPLIT)

    assert outcome is circle_router._Outcome.FAILED
    assert len(message.circles) == 1                       # circle 1 went out...
    assert message.answers[-1] == texts.circle_partial_failure(1, 3)  # ...and the user knows
    assert jobs.status == "failed"
    assert jobs.error_code == ProcessingErrorCode.ENCODE_FAILED.value
    assert workspaces_left(settings) == []                 # nothing left on disk
    assert file_server.deleted                             # server copy released too
    assert await state.get_state() is None


async def test_the_job_row_is_committed_before_a_long_encode(
    tmp_path, clean_env, state, session, monkeypatch, file_server
):
    """Early commit frees the DB connection, and the ORM objects stay usable."""
    from sqlalchemy import select

    from app.db.models import Job, JobStatus
    from app.db.repositories import JobsRepository

    settings = local_settings(tmp_path)
    service = FakeCircleService(150)
    commits_seen_during_encode = []
    original_encode = service.encode_segment

    async def spying_encode(*args, **kwargs):
        # Nothing left to commit: the row was persisted before encoding began.
        commits_seen_during_encode.append(not session.in_transaction())
        return await original_encode(*args, **kwargs)

    service.encode_segment = spying_encode
    monkeypatch.setattr(circle_router, "_circle_service", lambda s: service)
    monkeypatch.setattr(circle_router, "JobsRepository", JobsRepository)
    file_server.chunks = 1
    message = FakeMessage()

    await circle_router._run_circle_job(
        message=message, state=state, bot=FakeBot(SERVER_FILE, size=MB), settings=settings,
        session=session, incoming=video(size=MB), mode=circle_router.CircleMode.SPLIT,
        user_id=42,
    )
    await session.commit()

    assert commits_seen_during_encode[0] is True
    jobs_in_db = (await session.execute(select(Job))).scalars().all()
    assert [j.status for j in jobs_in_db] == [JobStatus.SUCCESS.value]
    assert jobs_in_db[0].output_size == 3 * 2048


# --- real ffmpeg ------------------------------------------------------------


@needs_ffmpeg
async def test_61_second_video_splits_into_two_real_circles(
    tmp_path, clean_env, state, jobs, video_factory
):
    source = video_factory(61)
    settings = local_settings(tmp_path, bot_api_local_dir=str(source.parent))
    message = FakeMessage(probe_circles=True)

    await run_job(settings, bot=FakeBot(str(source), size=source.stat().st_size),
                  message=message, state=state, incoming=video(size=source.stat().st_size),
                  mode=circle_router.CircleMode.SPLIT)

    assert jobs.status == "success", message.answers
    durations = [c["duration"] for c in message.circles]
    assert len(durations) == 2
    assert durations[0] == pytest.approx(60, abs=0.15)
    assert durations[1] == pytest.approx(1, abs=0.15)
    assert sum(durations) == pytest.approx(ffprobe(source).duration, abs=0.25)
    assert all(c["dimensions"] == (128, 128) and c["has_audio"] for c in message.circles)
    assert workspaces_left(settings) == []


@needs_ffmpeg
async def test_two_and_a_half_minute_video_becomes_three_real_circles(
    tmp_path, clean_env, state, jobs, video_factory
):
    source = video_factory(150)
    settings = local_settings(tmp_path, bot_api_local_dir=str(source.parent))
    message = FakeMessage(probe_circles=True)

    await run_job(settings, bot=FakeBot(str(source), size=source.stat().st_size),
                  message=message, state=state, incoming=video(size=source.stat().st_size),
                  mode=circle_router.CircleMode.SPLIT)

    durations = [c["duration"] for c in message.circles]
    assert [round(d) for d in durations] == [60, 60, 30]
    assert sum(durations) == pytest.approx(150, abs=0.3)


@needs_ffmpeg
async def test_exactly_sixty_seconds_is_one_real_circle(tmp_path, clean_env, state, jobs, video_factory):
    source = video_factory(60)
    settings = local_settings(tmp_path, bot_api_local_dir=str(source.parent))
    message = FakeMessage(probe_circles=True)

    outcome = await run_job(settings, bot=FakeBot(str(source), size=source.stat().st_size),
                            message=message, state=state,
                            incoming=video(size=source.stat().st_size),
                            mode=circle_router.CircleMode.AUTO)

    assert outcome is circle_router._Outcome.DONE
    assert len(message.circles) == 1
    assert message.circles[0]["duration"] == pytest.approx(60, abs=0.15)


@needs_ffmpeg
async def test_a_silent_long_video_splits_into_valid_silent_circles(
    tmp_path, clean_env, state, jobs, video_factory
):
    source = video_factory(61, audio=False)
    settings = local_settings(tmp_path, bot_api_local_dir=str(source.parent))
    message = FakeMessage(probe_circles=True)

    await run_job(settings, bot=FakeBot(str(source), size=source.stat().st_size),
                  message=message, state=state, incoming=video(size=source.stat().st_size),
                  mode=circle_router.CircleMode.SPLIT)

    assert jobs.status == "success", message.answers
    assert len(message.circles) == 2
    assert not any(c["has_audio"] for c in message.circles)
    assert all(c["dimensions"] == (128, 128) for c in message.circles)


@needs_ffmpeg
async def test_concurrent_jobs_for_different_users_stay_isolated(
    tmp_path, clean_env, monkeypatch, video_factory
):
    silent = video_factory(61, audio=False)
    loud = video_factory(90)
    settings = local_settings(tmp_path, bot_api_local_dir=str(silent.parent))
    gate = MediaJobGate(max_heavy_jobs=2, temp_root=settings.temp_root)
    recorders = {}
    monkeypatch.setattr(circle_router, "JobsRepository",
                        lambda session: recorders.setdefault(len(recorders), FakeJobs()))
    alice, bob = FakeMessage(1, probe_circles=True), FakeMessage(2, probe_circles=True)

    results = await asyncio.gather(
        run_job(settings, bot=FakeBot(str(silent), size=silent.stat().st_size), message=alice,
                state=fsm_for(1), incoming=video(size=silent.stat().st_size, file_id="A"),
                mode=circle_router.CircleMode.SPLIT, gate=gate, user_id=1),
        run_job(settings, bot=FakeBot(str(loud), size=loud.stat().st_size), message=bob,
                state=fsm_for(2), incoming=video(size=loud.stat().st_size, file_id="B"),
                mode=circle_router.CircleMode.SPLIT, gate=gate, user_id=2),
    )

    assert results == [circle_router._Outcome.DONE] * 2
    # Each user got exactly their own video back, cut their own way.
    assert [round(c["duration"]) for c in alice.circles] == [60, 1]
    assert [c["has_audio"] for c in alice.circles] == [False, False]
    assert [round(c["duration"]) for c in bob.circles] == [60, 30]
    assert [c["has_audio"] for c in bob.circles] == [True, True]
    assert all(r.status == "success" for r in recorders.values())
    # Both workspaces are gone and every slot and byte is handed back.
    assert workspaces_left(settings) == []
    assert gate.heavy_running == 0 and gate.reserved_bytes == 0


# --- admission control ------------------------------------------------------


def make_gate(**overrides) -> MediaJobGate:
    values = dict(max_heavy_jobs=2, temp_root=Path("/tmp/none"),
                  free_bytes=lambda _: 100 * 1024 * MB)
    values.update(overrides)
    return MediaJobGate(**values)


def test_heavy_jobs_are_capped_and_answered_immediately():
    gate = make_gate()
    first = gate.acquire(1, expected_bytes=MB, heavy=True)
    gate.acquire(2, expected_bytes=MB, heavy=True)
    with pytest.raises(ServerBusy) as excinfo:
        gate.acquire(3, expected_bytes=MB, heavy=True)
    assert excinfo.value.reason is BusyReason.SERVER

    first.release()
    gate.acquire(3, expected_bytes=MB, heavy=True)


def test_a_user_runs_one_heavy_job_at_a_time():
    gate = make_gate()
    gate.acquire(1, expected_bytes=MB, heavy=True)
    with pytest.raises(ServerBusy) as excinfo:
        gate.acquire(1, expected_bytes=MB, heavy=True)
    assert excinfo.value.reason is BusyReason.USER


def test_light_jobs_never_wait_on_heavy_slots():
    gate = make_gate(max_heavy_jobs=1)
    gate.acquire(1, expected_bytes=MB, heavy=True)
    for user in range(2, 12):
        gate.acquire(user, expected_bytes=MB, heavy=False)


def test_disk_reservations_protect_running_jobs():
    # 5 GB free minus the 512 MB margin leaves room for exactly two 2 GB jobs.
    gate = make_gate(max_heavy_jobs=8, free_bytes=lambda _: 5 * 1024 * MB)
    gate.acquire(1, expected_bytes=2048 * MB, heavy=True)
    gate.acquire(2, expected_bytes=2048 * MB, heavy=True)
    with pytest.raises(ServerBusy) as excinfo:
        gate.acquire(3, expected_bytes=2048 * MB, heavy=True)
    assert excinfo.value.reason is BusyReason.DISK
    assert gate.heavy_running == 2


def test_a_disk_budget_caps_total_reservations():
    gate = make_gate(disk_budget_bytes=100 * MB)
    ticket = gate.acquire(1, expected_bytes=80 * MB, heavy=False)
    with pytest.raises(ServerBusy):
        gate.acquire(2, expected_bytes=30 * MB, heavy=False)
    with ticket:
        pass  # leaving the block releases it
    gate.acquire(2, expected_bytes=30 * MB, heavy=False)


def test_releasing_twice_is_harmless():
    gate = make_gate()
    ticket = gate.acquire(1, expected_bytes=MB, heavy=True)
    ticket.release()
    ticket.release()
    assert gate.heavy_running == 0 and gate.reserved_bytes == 0


async def test_a_busy_server_is_a_friendly_reply_and_keeps_the_state(
    tmp_path, clean_env, state, jobs, monkeypatch
):
    settings = local_settings(tmp_path)
    gate = make_gate(max_heavy_jobs=1)
    gate.acquire(99, expected_bytes=MB, heavy=True)  # someone else's big job
    await state.set_state(CircleStates.choosing_long_mode)
    await state.update_data(circle_file=video(size=MB, duration=150).to_dict())
    message = FakeMessage()

    await circle_router.handle_long_video_choice(
        FakeCallback(message), CircleCallback(action="split"), state,
        bot=FakeBot(SERVER_FILE), settings=settings, session=None, media_gate=gate,
    )

    assert message.answers == [texts.SERVER_BUSY]
    assert jobs.created == []
    # The same buttons still work once the server frees up.
    assert await state.get_state() == CircleStates.choosing_long_mode.state


async def test_metadata_busy_reply_keeps_the_upload_state(tmp_path, clean_env, state, jobs):
    settings = local_settings(tmp_path)
    gate = make_gate(max_heavy_jobs=1)
    gate.acquire(42, expected_bytes=MB, heavy=True)  # this user's own running job
    await state.set_state(CleanMetadataStates.waiting_for_file)
    message = FakeMessage()
    message.document = SimpleNamespace(file_id="D", file_size=300 * MB,
                                       file_name="clip.mp4", mime_type="video/mp4")

    await metadata_router.handle_clean_file(
        message, state, bot=FakeBot(SERVER_FILE), settings=settings, session=None,
        media_gate=gate,
    )

    assert message.answers[-1] == texts.USER_JOB_RUNNING
    assert jobs.created == []
    assert await state.get_state() == CleanMetadataStates.waiting_for_file.state


# --- workspace hygiene ------------------------------------------------------


def test_startup_sweep_removes_only_leftover_workspaces(tmp_path):
    stale = tmp_path / str(uuid.uuid4())
    stale.mkdir()
    (stale / "src_huge.mp4").write_bytes(b"x" * 1024)
    unrelated = tmp_path / "keep-me"
    unrelated.mkdir()

    assert sweep_stale_workspaces(tmp_path) == 1
    assert not stale.exists()
    assert unrelated.exists()


def test_sweep_tolerates_a_missing_root(tmp_path):
    assert sweep_stale_workspaces(tmp_path / "absent") == 0


def test_workspace_usage_is_accounted(tmp_path):
    workspace = workspace_in(tmp_path)
    workspace.new_file(".mp4").write_bytes(b"x" * 1000)
    workspace.new_file(".mp4").write_bytes(b"x" * 24)
    assert workspace.usage_bytes() == 1024


def test_incoming_file_round_trips_through_fsm_storage():
    original = video(size=5 * MB, duration=61.0, name="clip.mov")
    assert IncomingFile.from_dict(json.loads(json.dumps(original.to_dict()))) == original
    assert IncomingFile.from_dict({"kind": "video"}) is None


async def test_get_file_waits_longer_for_a_bigger_file(tmp_path):
    """A local Bot API server has to move a multi-gigabyte upload into place
    before it can answer getFile; aiogram's one-minute default timed out and a
    1.5 GB video failed in production before any download started."""
    source = tmp_path / "cloud.mp4"
    source.write_bytes(b"v" * 2048)
    bot = FakeBot("videos/file_1.mp4", size=2048, cloud_source=source)
    service = TelegramFileService(bot, max_file_size_bytes=2000 * MB)

    await service.fetch(video(size=1500 * MB), workspace_in(tmp_path))

    (timeout,) = bot.get_file_timeouts
    assert timeout is not None and timeout > 60          # not aiogram's default
    assert timeout == transfer_timeout(1500 * MB)


async def test_a_getfile_timeout_is_reported_as_a_download_failure(tmp_path):
    """It used to land on the job row as "unknown_error"."""
    from aiogram.exceptions import TelegramNetworkError
    from aiogram.methods import GetFile

    class TimingOutBot(FakeBot):
        async def get_file(self, file_id, request_timeout=None):
            raise TelegramNetworkError(method=GetFile(file_id=file_id),
                                       message="Request timeout error")

    service = TelegramFileService(TimingOutBot("x"), max_file_size_bytes=2000 * MB)
    with pytest.raises(MediaProcessingError) as excinfo:
        await service.fetch(video(size=1500 * MB), workspace_in(tmp_path))

    assert excinfo.value.code is ProcessingErrorCode.DOWNLOAD_FAILED
    from app.bot.errors import error_code, user_message

    assert error_code(excinfo.value) == "download_failed"
    assert user_message(excinfo.value) == texts.ERROR_DOWNLOAD
