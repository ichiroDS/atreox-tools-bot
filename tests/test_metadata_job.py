"""The Metadata Studio job runner, driven end to end with real files on disk.

These exercise ``_run_metadata_job`` itself: the download, the working copy,
the workspace cleanup and the failure path. Telegram and the database are
faked; ExifTool is real.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot import texts
from app.bot.routers import metadata as metadata_router
from app.db.models import JobType
from app.services.media.base import MediaProcessingError, ProcessingErrorCode
from app.services.telegram_files import FileKind, IncomingFile
from app.utils.binaries import is_available, resolve_binary
from app.utils.subprocess import run_command

needs_tools = pytest.mark.skipif(
    not is_available("exiftool") or shutil.which("ffmpeg") is None,
    reason="exiftool/ffmpeg not installed",
)
EXIFTOOL = resolve_binary("exiftool")


class FakeMessage:
    def __init__(self):
        self.chat = SimpleNamespace(id=1)
        self.from_user = SimpleNamespace(id=42, is_bot=False)
        self.answers: list[str] = []
        self.documents: list = []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)
        return FakeMessage()

    async def answer_document(self, document=None, **kwargs):
        self.documents.append(document)
        return FakeMessage()

    async def delete(self):
        return True


class FakeBot:
    """Serves one local file as if Telegram had stored it."""

    def __init__(self, source: Path):
        self._source = source

    async def get_file(self, file_id):
        return SimpleNamespace(
            file_path=str(self._source), file_size=self._source.stat().st_size
        )

    async def download_file(self, file_path, destination):
        shutil.copy2(file_path, destination)


class FakeJobs:
    """Stands in for JobsRepository, recording the final job state."""

    def __init__(self):
        self.status = None
        self.error_code = None
        self.output_size = None

    async def create(self, **kwargs):
        self.created = kwargs
        return SimpleNamespace(id=kwargs["job_id"])

    async def mark_success(self, job, *, output_size=None):
        self.status = "success"
        self.output_size = output_size

    async def mark_failed(self, job, *, error_code):
        self.status = "failed"
        self.error_code = error_code


@pytest.fixture
def jobs(monkeypatch):
    recorder = FakeJobs()
    monkeypatch.setattr(metadata_router, "JobsRepository", lambda session: recorder)
    return recorder


@pytest_asyncio.fixture
async def state():
    context = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=42)
    )
    yield context
    await context.clear()


def settings_for(tmp_path: Path):
    return SimpleNamespace(
        temp_root=tmp_path / "workspaces",
        max_file_size_bytes=50 * 1024 * 1024,
        process_timeout_seconds=120,
        exiftool_bin="exiftool",
        exiftool_path=EXIFTOOL,
    )


async def make_jpeg(path: Path) -> Path:
    await run_command(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=size=320x240", "-frames:v", "1", str(path)],
        timeout=120,
    )
    await run_command(
        [EXIFTOOL, "-overwrite_original", "-EXIF:Make=Sony", "-EXIF:Artist=Someone",
         "--", str(path)],
        timeout=60,
    )
    return path


def incoming_for(name="IMG_0042.jpg"):
    return IncomingFile(
        file_id="FILE-1", kind=FileKind.IMAGE, size=1024, original_filename=name,
        mime_type="image/jpeg",
    )


async def run_clean(*, message, state, settings, source, jobs):
    service = metadata_router.MetadataService(
        exiftool_bin=settings.exiftool_path, timeout=settings.process_timeout_seconds
    )

    async def operation(workspace, working_copy):
        return await service.clean(working_copy, job_id=str(workspace.job_id))

    await metadata_router._run_metadata_job(
        message=message,
        state=state,
        bot=FakeBot(source),
        settings=settings,
        session=None,
        incoming=incoming_for(),
        job_type=JobType.METADATA_CLEAN,
        prefix="atreox",
        done_text=texts.METADATA_CLEAN_DONE,
        done_markup=None,
        operation=operation,
    )


@needs_tools
async def test_a_successful_job_sends_a_document_and_cleans_up(tmp_path, state, jobs):
    source = await make_jpeg(tmp_path / "original.jpg")
    original_bytes = source.read_bytes()
    settings = settings_for(tmp_path)
    message = FakeMessage()

    await run_clean(
        message=message, state=state, settings=settings, source=source, jobs=jobs
    )

    assert jobs.status == "success"
    assert message.documents, "the cleaned file was never sent"
    assert texts.METADATA_CLEAN_DONE in message.answers

    # The user's original is never modified - we only ever touch a copy.
    assert source.read_bytes() == original_bytes

    # And nothing is left behind on disk.
    assert list(settings.temp_root.glob("*")) == []


@needs_tools
async def test_the_result_is_named_after_the_original(tmp_path, state, jobs):
    source = await make_jpeg(tmp_path / "original.jpg")
    message = FakeMessage()

    await run_clean(
        message=message, state=state, settings=settings_for(tmp_path),
        source=source, jobs=jobs,
    )

    sent = message.documents[0]
    assert sent.filename == "atreox_IMG_0042.jpg"
    # A local path must never leak to the user.
    assert "\\" not in sent.filename and "/" not in sent.filename


@needs_tools
async def test_a_failing_job_reports_friendly_copy_and_still_cleans_up(
    tmp_path, state, jobs
):
    source = await make_jpeg(tmp_path / "original.jpg")
    settings = settings_for(tmp_path)
    message = FakeMessage()

    async def exploding_operation(workspace, working_copy):
        raise MediaProcessingError(ProcessingErrorCode.METADATA_FAILED, "boom")

    await metadata_router._run_metadata_job(
        message=message,
        state=state,
        bot=FakeBot(source),
        settings=settings,
        session=None,
        incoming=incoming_for(),
        job_type=JobType.METADATA_CLEAN,
        prefix="atreox",
        done_text=texts.METADATA_CLEAN_DONE,
        done_markup=None,
        operation=exploding_operation,
    )

    # The bot survives, the user gets copy rather than a traceback...
    assert texts.ERROR_PROCESSING in message.answers
    assert not message.documents
    # ...the failure is recorded...
    assert jobs.status == "failed"
    assert jobs.error_code == ProcessingErrorCode.METADATA_FAILED.value
    # ...the workspace is gone, and the wizard is reset.
    assert list(settings.temp_root.glob("*")) == []
    assert await state.get_state() is None


@needs_tools
async def test_a_corrupt_upload_does_not_crash_the_handler(tmp_path, state, jobs):
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"definitely not a jpeg")
    settings = settings_for(tmp_path)
    message = FakeMessage()

    await run_clean(
        message=message, state=state, settings=settings, source=broken, jobs=jobs
    )

    assert jobs.status == "failed"
    assert not message.documents
    assert message.answers[-1] == texts.ERROR_PROCESSING
    assert list(settings.temp_root.glob("*")) == []
