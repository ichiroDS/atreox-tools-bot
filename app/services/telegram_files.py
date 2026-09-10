"""Inspecting and downloading user-supplied Telegram files.

Nothing here trusts the client: the declared filename is only ever used to
derive a whitelisted extension, and size limits are enforced before we spend a
byte of disk.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.utils.temp_files import safe_display_name, safe_extension

logger = logging.getLogger(__name__)

VIDEO_MIME_TYPES = frozenset(
    {
        "video/mp4",
        "video/quicktime",
        "video/x-m4v",
        "video/webm",
        "video/x-matroska",
        "video/3gpp",
        "video/x-msvideo",
    }
)

IMAGE_MIME_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/heic",
        "image/heif",
        "image/webp",
        "image/tiff",
    }
)


class FileKind(str, enum.Enum):
    VIDEO = "video"
    IMAGE = "image"


class FileTooLargeError(Exception):
    def __init__(self, size: int, limit: int) -> None:
        super().__init__(f"file of {size} bytes exceeds limit {limit}")
        self.size = size
        self.limit = limit


@dataclass(frozen=True)
class IncomingFile:
    """A validated reference to a file the user just sent."""

    file_id: str
    kind: FileKind
    size: int | None = None
    original_filename: str | None = None
    mime_type: str | None = None
    # True when Telegram already re-encoded/compressed it for us.
    compressed: bool = False

    @property
    def extension(self) -> str:
        default = ".mp4" if self.kind is FileKind.VIDEO else ".jpg"
        return safe_extension(self.original_filename, default=default)

    def output_filename(self, *, prefix: str) -> str:
        fallback = f"{prefix}{self.extension}"
        name = safe_display_name(self.original_filename, fallback)
        return f"{prefix}_{name}" if name != fallback else fallback


def _document_kind(mime_type: str | None) -> FileKind | None:
    if not mime_type:
        return None
    mime_type = mime_type.lower()
    if mime_type in VIDEO_MIME_TYPES:
        return FileKind.VIDEO
    if mime_type in IMAGE_MIME_TYPES:
        return FileKind.IMAGE
    return None


def extract_video(message: Any) -> IncomingFile | None:
    """Pull a video out of a message: native video or a video document."""
    video = getattr(message, "video", None)
    if video is not None:
        return IncomingFile(
            file_id=video.file_id,
            kind=FileKind.VIDEO,
            size=getattr(video, "file_size", None),
            original_filename=getattr(video, "file_name", None),
            mime_type=getattr(video, "mime_type", None),
            compressed=True,
        )

    document = getattr(message, "document", None)
    if document is not None and _document_kind(getattr(document, "mime_type", None)) is FileKind.VIDEO:
        return IncomingFile(
            file_id=document.file_id,
            kind=FileKind.VIDEO,
            size=getattr(document, "file_size", None),
            original_filename=getattr(document, "file_name", None),
            mime_type=getattr(document, "mime_type", None),
        )
    return None


def extract_media(message: Any) -> IncomingFile | None:
    """Pull a photo or video out of a message, document form preferred."""
    document = getattr(message, "document", None)
    if document is not None:
        kind = _document_kind(getattr(document, "mime_type", None))
        if kind is not None:
            return IncomingFile(
                file_id=document.file_id,
                kind=kind,
                size=getattr(document, "file_size", None),
                original_filename=getattr(document, "file_name", None),
                mime_type=getattr(document, "mime_type", None),
            )
        return None

    video = extract_video(message)
    if video is not None:
        return video

    photos = getattr(message, "photo", None)
    if photos:
        largest = photos[-1]
        return IncomingFile(
            file_id=largest.file_id,
            kind=FileKind.IMAGE,
            size=getattr(largest, "file_size", None),
            original_filename=None,
            mime_type="image/jpeg",
            compressed=True,
        )
    return None


class TelegramFileService:
    """Downloads incoming files into a job workspace."""

    def __init__(self, bot: Any, *, max_file_size_bytes: int) -> None:
        self._bot = bot
        self._max_file_size_bytes = max_file_size_bytes

    def check_size(self, incoming: IncomingFile) -> None:
        if incoming.size and incoming.size > self._max_file_size_bytes:
            raise FileTooLargeError(incoming.size, self._max_file_size_bytes)

    async def download(
        self, incoming: IncomingFile, destination: Path, *, job_id: str | None = None
    ) -> Path:
        self.check_size(incoming)
        file = await self._bot.get_file(incoming.file_id)

        size = getattr(file, "file_size", None)
        if size and size > self._max_file_size_bytes:
            raise FileTooLargeError(size, self._max_file_size_bytes)

        await self._bot.download_file(file.file_path, destination=str(destination))
        logger.info(
            "downloaded %s bytes as %s",
            destination.stat().st_size if destination.exists() else 0,
            incoming.kind.value,
            extra={"job_id": job_id or "-"},
        )
        return destination
