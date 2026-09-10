from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from app.services.telegram_files import (
    FileKind,
    FileTooLargeError,
    IncomingFile,
    TelegramFileService,
    extract_media,
    extract_video,
)
from app.utils.subprocess import (
    CommandFailed,
    CommandNotFound,
    CommandTimeout,
    run_command,
)


# --- subprocess -------------------------------------------------------------


async def test_run_command_returns_stdout():
    result = await run_command(
        [sys.executable, "-c", "print(123)"], timeout=30
    )
    assert result.returncode == 0
    assert "123" in result.stdout


async def test_run_command_raises_on_non_zero_exit():
    with pytest.raises(CommandFailed) as excinfo:
        await run_command([sys.executable, "-c", "raise SystemExit(3)"], timeout=30)
    assert excinfo.value.returncode == 3


async def test_run_command_enforces_the_timeout():
    with pytest.raises(CommandTimeout):
        await run_command(
            [sys.executable, "-c", "import time; time.sleep(30)"], timeout=1
        )


async def test_run_command_reports_a_missing_binary():
    with pytest.raises(CommandNotFound):
        await run_command(["atreox-does-not-exist"], timeout=5)


async def test_run_command_rejects_an_empty_argv():
    with pytest.raises(ValueError):
        await run_command([], timeout=5)


async def test_run_command_does_not_use_a_shell():
    """A shell metacharacter must be passed through as a literal argument."""
    result = await run_command(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", "a; rm -rf /"],
        timeout=30,
    )
    assert "a; rm -rf /" in result.stdout


# --- incoming file extraction ----------------------------------------------


def message(**fields):
    base = {"video": None, "document": None, "photo": None}
    base.update(fields)
    return SimpleNamespace(**base)


def test_extract_video_from_a_native_video():
    incoming = extract_video(
        message(
            video=SimpleNamespace(
                file_id="V1", file_size=10, file_name=None, mime_type="video/mp4"
            )
        )
    )
    assert incoming.kind is FileKind.VIDEO
    assert incoming.compressed is True


def test_extract_video_from_a_video_document():
    incoming = extract_video(
        message(
            document=SimpleNamespace(
                file_id="D1", file_size=10, file_name="clip.mov",
                mime_type="video/quicktime",
            )
        )
    )
    assert incoming.kind is FileKind.VIDEO
    assert incoming.compressed is False
    assert incoming.extension == ".mov"


def test_extract_video_ignores_unrelated_documents():
    assert (
        extract_video(
            message(
                document=SimpleNamespace(
                    file_id="D1", file_size=10, file_name="notes.pdf",
                    mime_type="application/pdf",
                )
            )
        )
        is None
    )


def test_extract_video_ignores_photos():
    assert extract_video(message(photo=[SimpleNamespace(file_id="P1")])) is None


def test_extract_media_accepts_image_documents():
    incoming = extract_media(
        message(
            document=SimpleNamespace(
                file_id="D1", file_size=10, file_name="IMG_1.HEIC",
                mime_type="image/heic",
            )
        )
    )
    assert incoming.kind is FileKind.IMAGE
    assert incoming.extension == ".heic"


def test_extract_media_takes_the_largest_photo_size():
    incoming = extract_media(
        message(
            photo=[
                SimpleNamespace(file_id="small", file_size=10),
                SimpleNamespace(file_id="large", file_size=900),
            ]
        )
    )
    assert incoming.file_id == "large"
    assert incoming.compressed is True


def test_extract_media_returns_none_for_text():
    assert extract_media(message()) is None


def test_output_filename_drops_directories_and_unsafe_characters():
    incoming = IncomingFile(
        file_id="X", kind=FileKind.IMAGE, original_filename="../../secret photo.jpg"
    )
    assert incoming.output_filename(prefix="clean") == "clean_secret_photo.jpg"


def test_output_filename_falls_back_when_telegram_gave_no_name():
    incoming = IncomingFile(file_id="X", kind=FileKind.VIDEO)
    assert incoming.output_filename(prefix="clean") == "clean.mp4"


# --- size limits ------------------------------------------------------------


class FakeBot:
    def __init__(self, file_size=None):
        self.file_size = file_size
        self.downloaded = False

    async def get_file(self, file_id):
        return SimpleNamespace(file_path="path/on/telegram", file_size=self.file_size)

    async def download_file(self, file_path, destination):
        self.downloaded = True


async def test_declared_size_over_the_limit_is_rejected_before_download(tmp_path):
    bot = FakeBot()
    service = TelegramFileService(bot, max_file_size_bytes=1024)
    incoming = IncomingFile(file_id="X", kind=FileKind.VIDEO, size=2048)
    with pytest.raises(FileTooLargeError):
        await service.download(incoming, tmp_path / "out.mp4")
    assert bot.downloaded is False


async def test_size_reported_by_get_file_is_also_enforced(tmp_path):
    bot = FakeBot(file_size=5000)
    service = TelegramFileService(bot, max_file_size_bytes=1024)
    incoming = IncomingFile(file_id="X", kind=FileKind.VIDEO, size=None)
    with pytest.raises(FileTooLargeError):
        await service.download(incoming, tmp_path / "out.mp4")
    assert bot.downloaded is False


async def test_download_proceeds_within_the_limit(tmp_path):
    bot = FakeBot(file_size=100)
    service = TelegramFileService(bot, max_file_size_bytes=1024)
    incoming = IncomingFile(file_id="X", kind=FileKind.VIDEO, size=100)
    destination = tmp_path / "out.mp4"
    await service.download(incoming, destination)
    assert bot.downloaded is True
