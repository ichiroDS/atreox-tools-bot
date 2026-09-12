"""Phase 10: Batch Mode.

Collection is tested at the handler level - files one by one, album items,
duplicates, the maximum, the rate limiter - and processing is tested by
running real batches end to end with real ExifTool, FFmpeg and Pillow, so a
batch is held to the same standard as the single-file tools it reuses.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext
from sqlalchemy import func, select

from app.bot import texts
from app.bot.callbacks import BatchCallback, MenuCallback
from app.bot.keyboards.batch import batch_done, collecting, tool_choices
from app.bot.keyboards.common import main_menu
from app.bot.routers import batch as batch_router
from app.bot.states import BatchStates
from app.db.models import Feature, FeatureEvent, Job, JobStatus, JobType
from app.db.repositories import StatsRepository, WatermarkPresetsRepository
from app.services import telegram_files
from app.services.jobgate import MediaJobGate
from app.services.media.base import MediaProcessingError, ProcessingErrorCode
from app.services.media.batch import (
    BatchSummary,
    BatchTool,
    CleanMetadataProcessor,
    ItemOutcome,
    ItemOutput,
    ItemStatus,
    OptimizeProcessor,
    WatermarkProcessor,
)
from app.services.media.metadata import MetadataService
from app.services.media.optimizer import MediaOptimizerService, Preset
from app.services.media.watermark import Position, Size, Style, WatermarkService, WatermarkSpec
from app.services.ratelimit import RateLimiter
from app.utils.binaries import is_available, resolve_binary
from app.utils.temp_files import BATCH_PREFIX
from tests.test_large_files import MB, FakeBot, FakeJobs, local_settings, workspaces_left
from tests.test_media_optimizer import FakeCallback, FakeMessage, fsm_for, probe

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageChops  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)
EXIFTOOL = resolve_binary("exiftool")
needs_exiftool = pytest.mark.skipif(not is_available("exiftool"), reason="exiftool not installed")


# --- fakes ---------------------------------------------------------------------

MIME_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
              ".webp": "image/webp", ".mp4": "video/mp4", ".mov": "video/quicktime"}


class BatchMessage(FakeMessage):
    """A message with an id, so the collection status can be edited in place."""

    def __init__(self, user_id: int = 42, *, message_id: int = 1, keep: Path | None = None,
                 **media):
        super().__init__(user_id, keep=keep, **media)
        self.message_id = message_id

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)
        self.markups.append(kwargs.get("reply_markup"))
        return BatchMessage(self.from_user.id, message_id=900 + len(self.answers), parent=self)

    async def edit_text(self, text=None, **kwargs):
        (self.parent or self).edits.append(text)
        return self

    async def answer_document(self, document=None, **kwargs):
        # Like the shared fake, but a batch also carries placeholder files that
        # ffprobe has no business reading.
        path = Path(document.path)
        record = {
            "filename": document.filename,
            "no_detection": kwargs.get("disable_content_type_detection"),
            "size": path.stat().st_size,
            "workspace": path.parent,
        }
        if self.keep is not None:
            self.keep.mkdir(parents=True, exist_ok=True)
            record["copy"] = Path(shutil.copyfile(
                path, self.keep / f"{uuid.uuid4().hex}{path.suffix}"))
        if path.suffix.lower() in MIME_TYPES:
            record["probe"] = probe(path)
        self.documents.append(record)
        return SimpleNamespace(document=SimpleNamespace(file_name=document.filename))


class BatchBot(FakeBot):
    """Answers getFile per file id, and records status-message edits."""

    def __init__(self, sources: dict[str, Path] | None = None):
        super().__init__(file_path="", size=None)
        self.sources = sources or {}
        self.edits: list[str] = []

    async def get_file(self, file_id, request_timeout=None):
        path = self.sources[file_id]
        return SimpleNamespace(file_path=str(path), file_size=path.stat().st_size)

    async def edit_message_text(self, chat_id=None, message_id=None, text=None, **kwargs):
        self.edits.append(text)
        return SimpleNamespace(message_id=message_id)


class StubProcessor:
    """Stands in for a tool: copies the source, and can fail or skip on cue."""

    job_type = JobType.MEDIA_OPTIMIZE

    def __init__(self, *, fail_on: set[int] | None = None, skip_on: set[int] | None = None,
                 on_item=None):
        self.fail_on = fail_on or set()
        self.skip_on = skip_on or set()
        self.on_item = on_item
        self.seen: list[str] = []
        self.workspaces: list[Path] = []

    async def run(self, fetched, workspace, incoming, *, job_id=None):
        index = len(self.seen) + 1
        self.seen.append(incoming.file_id)
        self.workspaces.append(workspace.path)
        if self.on_item is not None:
            await self.on_item(index, workspace)
        if index in self.fail_on:
            raise MediaProcessingError(ProcessingErrorCode.ENCODE_FAILED, "boom")
        if index in self.skip_on:
            return ItemOutput(path=None, size_bytes=0, filename=f"skipped_{index}.bin", skipped=True)
        destination = workspace.new_file(".bin", prefix="out_")
        destination.write_bytes(b"processed")
        return ItemOutput(path=destination, size_bytes=destination.stat().st_size,
                          filename=f"atreox_done_{index}.bin")


# --- fixtures -------------------------------------------------------------------


@pytest.fixture(autouse=True)
def treat_fakes_as_messages(monkeypatch):
    monkeypatch.setattr(batch_router, "Message", BatchMessage)


@pytest.fixture(autouse=True)
def no_busy_waiting(monkeypatch):
    """The busy retry sleeps 5 s in production; tests should not."""
    monkeypatch.setattr(batch_router, "_BUSY_DELAY_SECONDS", 0)


@pytest.fixture
def instant_progress(monkeypatch):
    """Every item updates the progress message, so tests can see them all."""
    real = batch_router.BatchProgress
    monkeypatch.setattr(batch_router, "BatchProgress",
                        lambda status, total: real(status, total, min_interval=0))


@pytest.fixture
def jobs(monkeypatch):
    recorder = FakeJobs()
    monkeypatch.setattr(batch_router, "JobsRepository", lambda session: recorder)
    return recorder


def document_for(path: Path, index: int, *, size: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        file_id=f"F{index}", file_size=size if size is not None else path.stat().st_size,
        file_name=path.name, mime_type=MIME_TYPES[path.suffix.lower()],
    )


async def collect(paths: list[Path], *, state: FSMContext, bot: BatchBot, settings,
                  limiter: RateLimiter | None = None, user_id: int = 42) -> BatchMessage:
    """Send each file to the collector, as Telegram would deliver them."""
    await state.set_state(BatchStates.collecting)
    last = None
    for index, path in enumerate(paths):
        document = document_for(path, index)
        bot.sources[document.file_id] = path
        last = BatchMessage(user_id, message_id=100 + index, document=document)
        await batch_router.collect_file(last, state, bot=bot, settings=settings, limiter=limiter)
    return last


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    """Real photos and videos, made once and shared read-only."""
    root = tmp_path_factory.mktemp("batch-media")
    cache: dict[str, Path] = {}

    def photo(path: Path, colour: str = "black", size=(800, 600)) -> Path:
        picture = Image.new("RGB", size, colour)
        exif = Image.Exif()
        exif[0x010F] = "Samsung"          # Make
        exif[0x0110] = "SM-G991B"         # Model
        exif[0x8825] = {1: "N", 2: (50.0, 27.0, 0.0), 3: "E", 4: (30.0, 31.0, 0.0)}
        picture.save(path, quality=95, exif=exif.tobytes())
        return path

    def video(path: Path, *, size="640x360", seconds=1, bitrate="4M", audio=True,
              noisy=True) -> Path:
        args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30"]
        if audio:
            args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
        args += ["-t", str(seconds)]
        if noisy:
            # Noise makes the clip as hard to compress as real footage.
            args += ["-vf", "noise=alls=20:allf=t"]
        args += ["-c:v", "libx264", "-preset", "ultrafast", "-b:v", bitrate,
                 "-maxrate", bitrate, "-bufsize", bitrate, "-pix_fmt", "yuv420p"]
        args += ["-c:a", "aac"] if audio else ["-an"]
        subprocess.run([*args, str(path)], check=True, timeout=300)
        return path

    def get(name: str) -> Path:
        if name in cache:
            return cache[name]
        folder = root / name
        folder.mkdir()
        if name.startswith("photo"):
            path = photo(folder / f"{name}.jpg")
        elif name.startswith("hd_video"):
            path = video(folder / f"{name}.mp4", size="1920x1080", bitrate="6M")
        elif name.startswith("video"):
            path = video(folder / f"{name}.mp4")
        elif name == "lean_video":
            # Already about as small as this picture can be: no preset can
            # meaningfully shrink it, so a batch must keep the original.
            path = video(folder / "lean.mp4", size="320x240", seconds=2,
                         bitrate="40k", audio=False, noisy=False)
        elif name == "broken":
            path = folder / "broken.mp4"
            path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"not a video " * 500)
        else:
            raise AssertionError(name)
        cache[name] = path
        return path

    return get


def services(settings):
    return {
        "clean": CleanMetadataProcessor(MetadataService(
            exiftool_bin=EXIFTOOL, timeout=settings.process_timeout_seconds)),
        "optimizer": MediaOptimizerService(
            ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe",
            timeout=settings.process_timeout_seconds, probe_timeout=60),
        "watermark": WatermarkService(
            ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe",
            timeout=settings.process_timeout_seconds, probe_timeout=60),
    }


async def run(paths, processor, *, tmp_path, settings=None, state=None, session=None,
              gate=None, user_id=42, bot=None, keep=None):
    """Collect the files, then process them - the whole batch, as a user would."""
    settings = settings or local_settings(tmp_path)
    state = state or fsm_for(user_id)
    bot = bot or BatchBot()
    await collect(paths, state=state, bot=bot, settings=settings, user_id=user_id)
    message = BatchMessage(user_id, message_id=500, keep=keep or (tmp_path / f"out{user_id}"))
    summary = await batch_router.run_batch(
        message=message, state=state, bot=bot, settings=settings, session=session,
        processor=processor, user_id=user_id, media_gate=gate,
    )
    return summary, message, settings


# --- collection -----------------------------------------------------------------


async def test_files_sent_one_by_one_become_one_batch(tmp_path, clean_env, media):
    settings = local_settings(tmp_path)
    state = fsm_for()
    bot = BatchBot()
    paths = [media("photo1"), media("photo2"), media("video1")]

    await collect(paths, state=state, bot=bot, settings=settings)

    files = (await state.get_data())["files"]
    assert len(files) == 3
    assert [f["file_id"] for f in files] == ["F0", "F1", "F2"]
    # One status message, edited as the batch grew - not one message per file.
    assert bot.edits[-1] == texts.batch_collected(3, 20)
    assert len(bot.edits) == 2


async def test_an_album_lands_in_a_single_batch(tmp_path, clean_env, media):
    """Telegram delivers album items as separate updates with one group id."""
    settings = local_settings(tmp_path)
    state = fsm_for()
    bot = BatchBot()
    album = [media("photo1"), media("photo2"), media("photo3"), media("video1")]

    await state.set_state(BatchStates.collecting)
    for index, path in enumerate(album):
        document = document_for(path, index)
        bot.sources[document.file_id] = path
        message = BatchMessage(message_id=200 + index, document=document)
        message.media_group_id = "ALBUM-1"
        await batch_router.collect_file(message, state, bot=bot, settings=settings)

    assert len(((await state.get_data())["files"])) == 4
    assert await state.get_state() == BatchStates.collecting.state


async def test_a_redelivered_album_item_is_not_counted_twice(tmp_path, clean_env, media):
    settings = local_settings(tmp_path)
    state = fsm_for()
    bot = BatchBot()
    path = media("photo1")
    await state.set_state(BatchStates.collecting)
    document = document_for(path, 0)
    bot.sources[document.file_id] = path

    for _ in range(3):
        await batch_router.collect_file(
            BatchMessage(document=document), state, bot=bot, settings=settings)

    assert len((await state.get_data())["files"]) == 1


async def test_a_batch_stops_at_its_maximum(tmp_path, clean_env, media):
    settings = local_settings(tmp_path, max_batch_files=3)
    state = fsm_for()
    bot = BatchBot()
    path = media("photo1")
    await state.set_state(BatchStates.collecting)
    messages = []
    for index in range(5):
        document = document_for(path, index)
        bot.sources[document.file_id] = path
        message = BatchMessage(message_id=300 + index, document=document)
        messages.append(message)
        await batch_router.collect_file(message, state, bot=bot, settings=settings)

    assert len((await state.get_data())["files"]) == 3
    assert messages[-1].answers == [texts.batch_full(3)]
    assert "maximum of 3" in bot.edits[-1]


async def test_the_maximum_is_configurable(clean_env, tmp_path):
    assert local_settings(tmp_path).max_batch_files == 20
    assert local_settings(tmp_path, max_batch_files=35).max_batch_files == 35


async def test_a_twenty_file_batch_is_accepted(tmp_path, clean_env, media):
    settings = local_settings(tmp_path)
    state = fsm_for()
    bot = BatchBot()
    await collect([media("photo1")] * 20, state=state, bot=bot, settings=settings)
    assert len((await state.get_data())["files"]) == 20


async def test_the_rate_limiter_does_not_break_a_legitimate_batch(tmp_path, clean_env, media):
    """20 files arrive in seconds; the media bucket would have stopped at 8."""
    settings = local_settings(tmp_path)
    limiter = RateLimiter()
    state = fsm_for()
    bot = BatchBot()

    await collect([media("photo1")] * 20, state=state, bot=bot, settings=settings, limiter=limiter)

    assert len((await state.get_data())["files"]) == 20
    # Collecting never spent the media budget, so processing is still allowed.
    assert limiter.allow("media", 42) is True


async def test_an_upload_flood_is_still_limited(tmp_path, clean_env, media):
    settings = local_settings(tmp_path, max_batch_files=50)
    limiter = RateLimiter()
    state = fsm_for()
    bot = BatchBot()
    path = media("photo1")
    await state.set_state(BatchStates.collecting)

    refused = 0
    for index in range(120):
        document = document_for(path, index)
        bot.sources[document.file_id] = path
        message = BatchMessage(message_id=index, document=document)
        await batch_router.collect_file(message, state, bot=bot, settings=settings, limiter=limiter)
        refused += message.answers == [texts.RATE_LIMITED]
    assert refused > 0


async def test_unsupported_and_oversized_files_do_not_disturb_the_batch(tmp_path, clean_env, media):
    settings = local_settings(tmp_path)
    state = fsm_for()
    bot = BatchBot()
    await collect([media("photo1")], state=state, bot=bot, settings=settings)

    unsupported = BatchMessage(document=SimpleNamespace(
        file_id="PDF", file_size=10, file_name="a.pdf", mime_type="application/pdf"))
    await batch_router.collect_file(unsupported, state, bot=bot, settings=settings)
    huge = BatchMessage(document=SimpleNamespace(
        file_id="HUGE", file_size=2001 * MB, file_name="big.mp4", mime_type="video/mp4"))
    await batch_router.collect_file(huge, state, bot=bot, settings=settings)

    assert unsupported.answers == [texts.BATCH_UNSUPPORTED]
    assert huge.answers == [texts.ERROR_TOO_LARGE]
    assert len((await state.get_data())["files"]) == 1
    assert await state.get_state() == BatchStates.collecting.state


async def test_done_uploading_needs_at_least_one_file(tmp_path, clean_env):
    state = fsm_for()
    await state.set_state(BatchStates.collecting)
    await state.set_data({"files": []})
    callback = FakeCallback(BatchMessage())

    await batch_router.done_uploading(callback, state)

    assert callback.answers == [texts.BATCH_EMPTY]
    assert await state.get_state() == BatchStates.collecting.state


async def test_done_uploading_offers_the_three_batch_tools(tmp_path, clean_env, media):
    settings = local_settings(tmp_path)
    state = fsm_for()
    bot = BatchBot()
    await collect([media("photo1")], state=state, bot=bot, settings=settings)
    message = BatchMessage()

    await batch_router.done_uploading(FakeCallback(message), state)

    assert message.edits[-1] == texts.BATCH_CHOOSE_TOOL
    labels = [b.text for row in tool_choices().inline_keyboard for b in row]
    assert labels == ["🧹 Clean Metadata", "🖼 Add Watermark", "🗜 Optimize Media", "❌ Cancel"]
    assert await state.get_state() == BatchStates.choosing_tool.state


async def test_files_arriving_after_done_still_join_the_batch(tmp_path, clean_env, media):
    """An album's last item can land after the user has pressed Done."""
    settings = local_settings(tmp_path)
    state = fsm_for()
    bot = BatchBot()
    await collect([media("photo1")], state=state, bot=bot, settings=settings)
    await batch_router.done_uploading(FakeCallback(BatchMessage()), state)

    late = document_for(media("photo2"), 9)
    bot.sources[late.file_id] = media("photo2")
    await batch_router.collect_file(
        BatchMessage(document=late), state, bot=bot, settings=settings)

    assert len((await state.get_data())["files"]) == 2
    assert await state.get_state() == BatchStates.choosing_tool.state


async def test_cancelling_before_processing_clears_everything(tmp_path, clean_env, media):
    settings = local_settings(tmp_path)
    state = fsm_for()
    bot = BatchBot()
    await collect([media("photo1")], state=state, bot=bot, settings=settings)
    message = BatchMessage()
    callback = FakeCallback(message)

    await batch_router.cancel_batch(callback, state)

    assert await state.get_state() is None
    assert await state.get_data() == {}
    assert callback.answers == [texts.CANCELLED]
    assert message.edits[-1] == texts.MAIN_MENU


async def test_files_sent_while_a_batch_runs_are_answered(tmp_path, clean_env):
    message = BatchMessage()
    await batch_router.busy_with_a_batch(message)
    assert message.answers == [texts.BATCH_RUNNING]


# --- processing: the orchestration ------------------------------------------------


async def test_a_one_file_batch(tmp_path, clean_env, media, jobs, instant_progress):
    processor = StubProcessor()
    summary, message, settings = await run([media("photo1")], processor, tmp_path=tmp_path)

    assert summary == BatchSummary(total=1, sent=1, skipped=0, failed=0)
    assert [d["filename"] for d in message.documents] == ["atreox_done_1.bin"]
    assert message.answers[-1] == texts.batch_summary(total=1, successful=1, failed=0)
    assert workspaces_left(settings) == []


async def test_a_twenty_file_batch_processes_every_item(tmp_path, clean_env, media, jobs):
    processor = StubProcessor()
    summary, message, settings = await run([media("photo1")] * 20, processor, tmp_path=tmp_path)

    assert summary.total == summary.sent == 20
    assert len(message.documents) == 20
    assert len(processor.seen) == 20
    assert workspaces_left(settings) == []


async def test_one_failed_item_does_not_stop_the_others(tmp_path, clean_env, media, jobs,
                                                        instant_progress):
    processor = StubProcessor(fail_on={2})
    summary, message, settings = await run([media("photo1")] * 4, processor, tmp_path=tmp_path)

    assert summary == BatchSummary(total=4, sent=3, skipped=0, failed=1)
    assert len(message.documents) == 3
    assert len(processor.seen) == 4                      # the batch kept going
    assert message.answers[-1] == texts.batch_summary(total=4, successful=3, failed=1)
    assert jobs.status in ("success", "failed")
    assert workspaces_left(settings) == []


async def test_a_skipped_item_keeps_the_original_and_is_reported(tmp_path, clean_env, media, jobs):
    processor = StubProcessor(skip_on={1})
    summary, message, _ = await run([media("photo1")] * 3, processor, tmp_path=tmp_path)

    assert summary == BatchSummary(total=3, sent=2, skipped=1, failed=0)
    assert len(message.documents) == 2                   # nothing sent for the skipped one
    assert message.answers[-1] == texts.batch_summary(
        total=3, successful=3, failed=0, skipped=1)
    assert "Already efficient" in message.answers[-1]


async def test_each_item_gets_its_own_directory_in_one_batch_workspace(
    tmp_path, clean_env, media, jobs
):
    processor = StubProcessor()
    _, _, settings = await run([media("photo1")] * 3, processor, tmp_path=tmp_path)

    names = [path.name for path in processor.workspaces]
    assert names == ["item_001", "item_002", "item_003"]
    parents = {path.parent for path in processor.workspaces}
    assert len(parents) == 1
    batch_root = parents.pop()
    assert batch_root.name.startswith(BATCH_PREFIX)
    assert batch_root.parent == Path(settings.temp_root)
    # Everything is gone once the batch ends.
    assert not batch_root.exists()
    assert workspaces_left(settings) == []


async def test_the_workspace_is_removed_even_when_every_item_fails(
    tmp_path, clean_env, media, jobs
):
    processor = StubProcessor(fail_on={1, 2})
    summary, _, settings = await run([media("photo1")] * 2, processor, tmp_path=tmp_path)
    assert summary.failed == 2
    assert workspaces_left(settings) == []


async def test_cancelling_mid_run_stops_after_the_current_file(tmp_path, clean_env, media, jobs):
    state = fsm_for()

    async def cancel_after_first(index, workspace):
        if index == 1:
            await batch_router.cancel_batch(FakeCallback(BatchMessage()), state)

    processor = StubProcessor(on_item=cancel_after_first)
    summary, message, settings = await run(
        [media("photo1")] * 4, processor, tmp_path=tmp_path, state=state)

    assert summary.cancelled is True
    assert len(processor.seen) == 1
    assert message.answers[-1].startswith("📦 Batch stopped")
    assert await state.get_state() is None
    assert workspaces_left(settings) == []


async def test_progress_is_one_message_that_gets_edited(tmp_path, clean_env, media, jobs,
                                                        instant_progress):
    processor = StubProcessor()
    _, message, _ = await run([media("photo1")] * 3, processor, tmp_path=tmp_path)

    # One "0 / 3" message, then an edit per finished file - not four messages.
    assert texts.batch_progress(0, 3) in message.answers
    assert message.edits == [texts.batch_progress(index, 3) for index in (1, 2, 3)]
    assert sum(1 for answer in message.answers if "Processing batch" in answer) == 1


def test_progress_updates_are_throttled():
    class Clock:
        now = 0.0

        def __call__(self):
            return self.now

    status = BatchMessage()
    clock = Clock()
    progress = batch_router.BatchProgress(status, 5, min_interval=2.0, clock=clock)

    async def run_updates():
        for done, at in ((1, 0.5), (2, 1.0), (3, 3.0), (4, 3.5), (5, 3.6)):
            clock.now = at
            await progress.update(done)

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(run_updates())
    # Throttled in between, but the final count always shows.
    assert status.edits == [texts.batch_progress(3, 5), texts.batch_progress(5, 5)]


# --- processing: concurrency and isolation -----------------------------------------


async def test_two_users_run_their_own_batches_without_mixing(tmp_path, clean_env, media,
                                                              monkeypatch):
    recorders: dict[int, FakeJobs] = {}
    monkeypatch.setattr(batch_router, "JobsRepository",
                        lambda session: recorders.setdefault(len(recorders), FakeJobs()))
    gate = MediaJobGate(max_heavy_jobs=2, temp_root=tmp_path, free_bytes=lambda _: 100 * 1024 * MB)
    settings = local_settings(tmp_path)
    alice, bob = StubProcessor(), StubProcessor()

    results = await asyncio.gather(
        run([media("photo1")] * 3, alice, tmp_path=tmp_path, settings=settings,
            state=fsm_for(1), user_id=1, gate=gate),
        run([media("photo2")] * 2, bob, tmp_path=tmp_path, settings=settings,
            state=fsm_for(2), user_id=2, gate=gate),
    )

    (alice_summary, alice_message, _), (bob_summary, bob_message, _) = results
    assert alice_summary.sent == 3 and bob_summary.sent == 2
    assert len(alice_message.documents) == 3 and len(bob_message.documents) == 2
    # Separate batch workspaces, both cleaned up.
    assert {p.parent for p in alice.workspaces} != {p.parent for p in bob.workspaces}
    assert workspaces_left(settings) == []
    assert gate.heavy_running == 0 and gate.reserved_bytes == 0


async def test_a_batch_never_runs_two_heavy_jobs_at_once(tmp_path, clean_env, media, jobs):
    """Sequential by design: one 4K encode must not invite a second."""
    gate = MediaJobGate(max_heavy_jobs=2, temp_root=tmp_path, free_bytes=lambda _: 100 * 1024 * MB)
    running: list[int] = []

    async def watch(index, workspace):
        running.append(gate.heavy_running)

    processor = StubProcessor(on_item=watch)
    settings = local_settings(tmp_path)
    state = fsm_for()
    bot = BatchBot()
    path = media("photo1")
    await state.set_state(BatchStates.collecting)
    for index in range(4):
        document = document_for(path, index, size=200 * MB)   # heavy by size
        bot.sources[document.file_id] = path
        await batch_router.collect_file(
            BatchMessage(document=document), state, bot=bot, settings=settings)

    await batch_router.run_batch(
        message=BatchMessage(keep=tmp_path / "out"), state=state, bot=bot, settings=settings,
        session=None, processor=processor, user_id=42, media_gate=gate,
    )

    assert running == [1, 1, 1, 1]      # never two at the same time
    assert gate.heavy_running == 0


async def test_an_item_waits_for_a_busy_server_then_gives_up_without_killing_the_batch(
    tmp_path, clean_env, media, jobs
):
    gate = MediaJobGate(max_heavy_jobs=1, temp_root=tmp_path, free_bytes=lambda _: 100 * 1024 * MB)
    gate.acquire(99, expected_bytes=MB, heavy=True)        # someone else's big job
    settings = local_settings(tmp_path)
    state = fsm_for()
    bot = BatchBot()
    path = media("photo1")
    await state.set_state(BatchStates.collecting)
    for index in range(2):
        document = document_for(path, index, size=200 * MB)
        bot.sources[document.file_id] = path
        await batch_router.collect_file(
            BatchMessage(document=document), state, bot=bot, settings=settings)

    message = BatchMessage(keep=tmp_path / "out")
    summary = await batch_router.run_batch(
        message=message, state=state, bot=bot, settings=settings, session=None,
        processor=StubProcessor(), user_id=42, media_gate=gate,
    )

    assert summary == BatchSummary(total=2, sent=0, skipped=0, failed=2)
    assert message.answers[-1] == texts.batch_summary(total=2, successful=0, failed=2)
    assert workspaces_left(settings) == []


# --- processing: the real tools ----------------------------------------------------


@needs_exiftool
async def test_clean_metadata_batch(tmp_path, clean_env, media, jobs, instant_progress):
    sources = [media("photo1"), media("photo2"), media("photo3")]
    settings = local_settings(tmp_path)
    processor = services(settings)["clean"]

    summary, message, _ = await run(sources, processor, tmp_path=tmp_path, settings=settings,
                                    keep=tmp_path / "cleaned")

    assert summary == BatchSummary(total=3, sent=3, skipped=0, failed=0)
    assert [d["filename"] for d in message.documents] == [
        f"atreox_cleaned_{path.name}" for path in sources]
    for document, source in zip(message.documents, sources):
        with Image.open(document["copy"]) as cleaned, Image.open(source) as original:
            assert cleaned.size == original.size          # dimensions preserved
            assert cleaned.getexif().get(0x010F) is None  # Make gone
            assert not cleaned.getexif().get_ifd(0x8825)  # GPS gone
        # The Bot API server's own file was copied, never edited in place.
        with Image.open(source) as untouched:
            assert untouched.getexif().get(0x010F) == "Samsung"
    assert workspaces_left(settings) == []


@needs_ffmpeg
async def test_watermark_batch_uses_one_saved_preset_for_every_file(
    tmp_path, clean_env, media, jobs, session
):
    settings = local_settings(tmp_path)
    preset = await WatermarkPresetsRepository(session).create(
        42, name="@lunaaa", text="@lunaaa", position=Position.BOTTOM_RIGHT.value,
        style=Style.WHITE.value, size=Size.L.value, opacity=100,
    )
    spec = WatermarkSpec.from_dict({
        "text": preset.text, "position": preset.position, "style": preset.style,
        "size": preset.size, "opacity": preset.opacity,
    })
    processor = WatermarkProcessor(services(settings)["watermark"], spec)
    sources = [media("photo1"), media("video1")]

    summary, message, _ = await run(sources, processor, tmp_path=tmp_path, settings=settings,
                                    keep=tmp_path / "marked")

    assert summary == BatchSummary(total=2, sent=2, skipped=0, failed=0)
    assert [d["filename"] for d in message.documents] == [
        "atreox_watermarked_photo1.jpg", "atreox_watermarked_video1.mp4"]
    # The same watermark really landed on the photo, in the chosen corner.
    with Image.open(message.documents[0]["copy"]) as marked, Image.open(sources[0]) as original:
        assert marked.size == original.size
        box = ImageChops.difference(marked.convert("RGB"), original.convert("RGB")).getbbox()
        assert box is not None and box[0] > marked.width * 0.4 and box[3] > marked.height * 0.4
    video = message.documents[1]["probe"]
    assert video["video_codec"] == "h264" and video["audio_codecs"] == ["aac"]
    assert video["duration"] == pytest.approx(1, abs=0.2)


@needs_ffmpeg
@pytest.mark.parametrize("preset,expected", [
    (Preset.SMALL, (1280, 720)),        # Small caps the short side at 720...
    (Preset.BALANCED, (1920, 1080)),    # ...the other two at 1080.
    (Preset.HIGH, (1920, 1080)),
])
async def test_optimizer_batch_applies_one_preset_to_every_file(
    preset, expected, tmp_path, clean_env, media, jobs
):
    settings = local_settings(tmp_path)
    processor = OptimizeProcessor(services(settings)["optimizer"], preset)
    sources = [media("hd_video1"), media("hd_video2")]

    summary, message, _ = await run(sources, processor, tmp_path=tmp_path, settings=settings,
                                    keep=tmp_path / f"opt-{preset.value}")

    assert summary == BatchSummary(total=2, sent=2, skipped=0, failed=0)
    for document, source in zip(message.documents, sources):
        assert document["filename"].startswith("atreox_optimized_")
        assert (document["probe"]["width"], document["probe"]["height"]) == expected
        assert document["size"] < source.stat().st_size      # never larger
    assert workspaces_left(settings) == []


@needs_ffmpeg
async def test_an_already_efficient_file_keeps_its_original(tmp_path, clean_env, media, jobs):
    settings = local_settings(tmp_path)
    processor = OptimizeProcessor(services(settings)["optimizer"], Preset.BALANCED)

    summary, message, _ = await run([media("lean_video"), media("video1")], processor,
                                    tmp_path=tmp_path, settings=settings,
                                    keep=tmp_path / "mixed")

    assert summary.skipped == 1 and summary.sent == 1
    assert len(message.documents) == 1                       # the lean one was not replaced
    assert "Already efficient (original kept): 1" in message.answers[-1]


@needs_ffmpeg
async def test_a_mixed_batch_of_photos_and_videos_with_one_broken_file(
    tmp_path, clean_env, media, jobs, instant_progress
):
    settings = local_settings(tmp_path)
    processor = OptimizeProcessor(services(settings)["optimizer"], Preset.SMALL)
    sources = [media("photo1"), media("broken"), media("video1")]

    summary, message, _ = await run(sources, processor, tmp_path=tmp_path, settings=settings,
                                    keep=tmp_path / "mixed2")

    assert summary == BatchSummary(total=3, sent=2, skipped=0, failed=1)
    kinds = [Path(d["filename"]).suffix for d in message.documents]
    assert sorted(kinds) == [".jpg", ".mp4"]
    assert message.answers[-1] == texts.batch_summary(total=3, successful=2, failed=1)
    assert workspaces_left(settings) == []


# --- database and analytics ---------------------------------------------------------


async def test_a_batch_records_one_job_per_file_and_both_batch_events(
    tmp_path, clean_env, media, session
):
    settings = local_settings(tmp_path)
    processor = StubProcessor(fail_on={2})

    await run([media("photo1")] * 3, processor, tmp_path=tmp_path, settings=settings,
              session=session)
    await session.commit()

    jobs_rows = (await session.execute(select(Job))).scalars().all()
    assert len(jobs_rows) == 3                                   # one per file, not one per batch
    assert {row.type for row in jobs_rows} == {JobType.MEDIA_OPTIMIZE.value}
    assert sorted(row.status for row in jobs_rows) == [
        JobStatus.FAILED.value, JobStatus.SUCCESS.value, JobStatus.SUCCESS.value]

    events = (await session.execute(select(FeatureEvent.feature))).scalars().all()
    assert sorted(events) == ["batch_completed", "batch_started"]

    stats = await StatsRepository(session).collect()
    assert stats.batches == 1
    report = texts.stats_report(stats)
    assert "📦 Batches: 1" in report
    assert report.index("🖼 Watermarks") < report.index("📦 Batches")


# --- menu, help, copy ---------------------------------------------------------------


def test_batch_mode_is_in_the_main_menu():
    buttons = [b for row in main_menu().inline_keyboard for b in row]
    labels = [b.text for b in buttons]
    assert labels.index("📦 Batch Mode") == labels.index("🖼 Watermark") + 1
    assert buttons[labels.index("📦 Batch Mode")].callback_data == \
        MenuCallback(action="batch").pack()


def test_the_prompt_is_the_specified_copy():
    assert texts.BATCH_PROMPT == (
        "📦 Send me multiple photos or videos.\n\n"
        "You can send them one by one or as a Telegram album.\n\n"
        "When you're done, press:\n"
        "✅ Done Uploading"
    )
    assert [b.text for row in collecting().inline_keyboard for b in row] == [
        "✅ Done Uploading", "❌ Cancel"]


def test_help_describes_batch_mode():
    assert ("📦 <b>Batch Mode</b>\nProcess multiple photos and videos at once with Metadata "
            "Cleaner, Watermark or Media Optimizer.") in texts.HELP


def test_the_collection_and_summary_copy():
    assert texts.batch_collected(7, 20) == (
        "📦 <b>Batch</b>\n7 files received\n\nSend more or press Done Uploading.")
    assert texts.batch_collected(1, 20).splitlines()[1] == "1 file received"
    assert texts.batch_progress(3, 12) == "⏳ <b>Processing batch</b>\n\n3 / 12 completed"
    assert texts.batch_summary(total=12, successful=11, failed=1) == (
        "✅ Batch complete\n\nFiles: 12\nSuccessful: 11\nFailed: 1")


def test_the_done_keyboard_offers_a_new_batch_and_the_menu():
    assert [b.text for row in batch_done().inline_keyboard for b in row] == [
        "📦 New Batch", "🏠 Main Menu"]


def test_every_batch_callback_fits_telegrams_budget():
    for markup in (collecting(), tool_choices(), batch_done()):
        for row in markup.inline_keyboard:
            for button in row:
                assert len(button.callback_data.encode("utf-8")) <= 64


def test_batch_supports_exactly_three_tools():
    assert [tool.value for tool in BatchTool] == ["clean", "watermark", "optimize"]


def test_the_summary_counts_a_skip_as_a_success():
    summary = BatchSummary.of(3, [
        ItemOutcome(1, ItemStatus.SENT), ItemOutcome(2, ItemStatus.SKIPPED),
        ItemOutcome(3, ItemStatus.FAILED),
    ])
    assert (summary.sent, summary.skipped, summary.failed) == (1, 1, 1)
    assert summary.successful == 2
