"""Inspecting and fetching user-supplied Telegram files.

Nothing here trusts the client: the declared filename is only ever used to
derive a whitelisted extension, and size limits are enforced before we spend a
byte of disk - and again while bytes are arriving.

``getFile`` answers in one of three shapes, and each is handled without
loading the file into memory:

* **Cloud Bot API** - a relative ``file_path``; the bytes are streamed from
  ``api.telegram.org`` into the job workspace.
* **Local Bot API, shared filesystem** - an absolute path this process can
  read. It is used in place: no copy at all.
* **Local Bot API, separate container** - an absolute path on the Bot API
  server's disk. It is mapped onto that server's private file endpoint
  (``BOT_API_FILES_URL``) and streamed into the workspace.
"""

from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator, Callable, Protocol
from urllib.parse import quote

import aiofiles

from app.services.media.base import MediaProcessingError, ProcessingErrorCode
from app.utils.temp_files import JobWorkspace, safe_display_name, safe_extension

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

# Written to disk a megabyte at a time: large enough to be efficient, small
# enough that a multi-GB file never occupies meaningful memory.
CHUNK_SIZE = 1024 * 1024

# Seconds of silence tolerated mid-transfer before a stream is abandoned. There
# is deliberately no *total* timeout: a 2 GB file may legitimately take a while.
_STREAM_IDLE_TIMEOUT = 120
_STREAM_CONNECT_TIMEOUT = 15


def transfer_timeout(size_bytes: int | None) -> int:
    """A request timeout that grows with the payload.

    Two minutes of slack plus two seconds per megabyte (a pessimistic
    0.5 MB/s), capped at two hours.
    """
    size_mb = (size_bytes or 0) / (1024 * 1024)
    return int(min(120 + size_mb * 2, 7200))


class FileKind(str, enum.Enum):
    VIDEO = "video"
    IMAGE = "image"


class FileTooLargeError(Exception):
    def __init__(self, size: int, limit: int) -> None:
        super().__init__(f"file of {size} bytes exceeds limit {limit}")
        self.size = size
        self.limit = limit


class OutputTooLargeError(Exception):
    """A produced file is bigger than the Bot API server will accept."""

    def __init__(self, size: int, limit: int) -> None:
        super().__init__(f"output of {size} bytes exceeds send limit {limit}")
        self.size = size
        self.limit = limit


def ensure_sendable(size_bytes: int, limit_bytes: int) -> None:
    """Refuse to start an upload that is certain to fail."""
    if size_bytes > limit_bytes:
        raise OutputTooLargeError(size_bytes, limit_bytes)


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
    # Seconds, as Telegram declared it. Only native videos carry one.
    duration: float | None = None

    @property
    def extension(self) -> str:
        default = ".mp4" if self.kind is FileKind.VIDEO else ".jpg"
        return safe_extension(self.original_filename, default=default)

    def output_filename(self, *, prefix: str) -> str:
        fallback = f"{prefix}{self.extension}"
        name = safe_display_name(self.original_filename, fallback)
        return f"{prefix}_{name}" if name != fallback else fallback

    def exceeds(self, limit_bytes: int) -> bool:
        return bool(self.size and self.size > limit_bytes)

    def to_dict(self) -> dict:
        """FSM-storable form, so a choice made later can refer back to it."""
        return {
            "file_id": self.file_id,
            "kind": self.kind.value,
            "size": self.size,
            "original_filename": self.original_filename,
            "mime_type": self.mime_type,
            "compressed": self.compressed,
            "duration": self.duration,
        }

    @classmethod
    def from_dict(cls, payload: dict | None) -> IncomingFile | None:
        if not payload:
            return None
        try:
            return cls(
                file_id=payload["file_id"],
                kind=FileKind(payload["kind"]),
                size=payload.get("size"),
                original_filename=payload.get("original_filename"),
                mime_type=payload.get("mime_type"),
                compressed=bool(payload.get("compressed", False)),
                duration=payload.get("duration"),
            )
        except (KeyError, ValueError):
            return None


def _document_kind(mime_type: str | None) -> FileKind | None:
    if not mime_type:
        return None
    mime_type = mime_type.lower()
    if mime_type in VIDEO_MIME_TYPES:
        return FileKind.VIDEO
    if mime_type in IMAGE_MIME_TYPES:
        return FileKind.IMAGE
    return None


def _declared_duration(media: Any) -> float | None:
    raw = getattr(media, "duration", None)
    return float(raw) if isinstance(raw, (int, float)) and raw > 0 else None


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
            duration=_declared_duration(video),
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


# --- where the bytes live ---------------------------------------------------


class SourceKind(str, enum.Enum):
    CLOUD = "cloud"        # relative path on api.telegram.org
    LOCAL_PATH = "local"   # absolute path readable right here
    LOCAL_HTTP = "http"    # absolute path on the Bot API server, served over HTTP


@dataclass(frozen=True)
class FileLocation:
    kind: SourceKind
    file_path: str
    url: str | None = None


def is_absolute_server_path(file_path: str) -> bool:
    """Local mode returns absolute paths; the cloud API never does."""
    return file_path.startswith("/") or os.path.isabs(file_path)


def resolve_file_location(
    file_path: str,
    *,
    local_mode: bool,
    local_dir: str,
    files_base_url: str | None,
    path_exists: Callable[[str], bool] = os.path.isfile,
) -> FileLocation:
    """Decide how to reach the bytes behind a ``getFile`` answer.

    Pure apart from the injectable existence check, so every branch is unit
    tested without a Bot API server.
    """
    if not file_path:
        raise MediaProcessingError(ProcessingErrorCode.DOWNLOAD_FAILED, "empty file_path")

    if not is_absolute_server_path(file_path):
        return FileLocation(SourceKind.CLOUD, file_path)

    if not local_mode:
        # Only a local server hands out absolute paths. Refusing here means a
        # misconfigured deployment fails loudly instead of reading local disk.
        raise MediaProcessingError(
            ProcessingErrorCode.DOWNLOAD_FAILED, "absolute file_path from a cloud Bot API"
        )

    if path_exists(file_path):
        return FileLocation(SourceKind.LOCAL_PATH, file_path)

    if files_base_url:
        server_root = PurePosixPath(local_dir)
        try:
            relative = PurePosixPath(file_path).relative_to(server_root)
        except ValueError as exc:
            raise MediaProcessingError(
                ProcessingErrorCode.DOWNLOAD_FAILED,
                "file_path outside the Bot API working directory",
            ) from exc
        if ".." in relative.parts:
            raise MediaProcessingError(ProcessingErrorCode.DOWNLOAD_FAILED, "unsafe file_path")
        url = f"{files_base_url.rstrip('/')}/{quote(relative.as_posix(), safe='/')}"
        return FileLocation(SourceKind.LOCAL_HTTP, file_path, url=url)

    raise MediaProcessingError(
        ProcessingErrorCode.DOWNLOAD_FAILED,
        "local Bot API path is not on this filesystem and BOT_API_FILES_URL is unset",
    )


@dataclass(frozen=True)
class FetchedFile:
    """A source file ready for processing."""

    path: Path
    size: int
    location: FileLocation

    @property
    def borrowed(self) -> bool:
        """True when ``path`` belongs to the Bot API server: read it, never edit it."""
        return self.location.kind is SourceKind.LOCAL_PATH


class HttpFileClient(Protocol):
    def stream(self, url: str, *, chunk_size: int) -> AsyncIterator[bytes]: ...

    async def delete(self, url: str) -> None: ...


class AiohttpFileClient:
    """Streams from, and cleans up on, the local Bot API file endpoint."""

    def stream(self, url: str, *, chunk_size: int) -> AsyncIterator[bytes]:
        return self._stream(url, chunk_size)

    async def _stream(self, url: str, chunk_size: int) -> AsyncIterator[bytes]:
        import aiohttp

        timeout = aiohttp.ClientTimeout(
            total=None, sock_connect=_STREAM_CONNECT_TIMEOUT, sock_read=_STREAM_IDLE_TIMEOUT
        )
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, raise_for_status=True) as response:
                async for chunk in response.content.iter_chunked(chunk_size):
                    yield chunk

    async def delete(self, url: str) -> None:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.delete(url) as response:
                if response.status >= 400 and response.status != 404:
                    raise RuntimeError(f"file server answered {response.status}")


async def stream_to_file(
    chunks: AsyncIterator[bytes], destination: Path, *, limit_bytes: int
) -> int:
    """Write an async byte stream to disk, aborting past ``limit_bytes``.

    Only one chunk is ever held in memory. A partial file is removed on any
    failure so it never counts against the workspace.
    """
    written = 0
    try:
        async with aiofiles.open(destination, "wb") as handle:
            async for chunk in chunks:
                written += len(chunk)
                if written > limit_bytes:
                    raise FileTooLargeError(written, limit_bytes)
                await handle.write(chunk)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        # Closes the HTTP response promptly when we stop reading early.
        close = getattr(chunks, "aclose", None)
        if close is not None:
            await close()
    return written


class TelegramFileService:
    """Brings incoming files into a job workspace (or borrows them in place)."""

    def __init__(
        self,
        bot: Any,
        *,
        max_file_size_bytes: int,
        local_mode: bool = False,
        local_dir: str = "/var/lib/telegram-bot-api",
        files_base_url: str | None = None,
        http: HttpFileClient | None = None,
        chunk_size: int = CHUNK_SIZE,
    ) -> None:
        self._bot = bot
        self._max_file_size_bytes = max_file_size_bytes
        self._local_mode = local_mode
        self._local_dir = local_dir
        self._files_base_url = files_base_url
        self._http = http or AiohttpFileClient()
        self._chunk_size = chunk_size

    @classmethod
    def from_settings(cls, bot: Any, settings: Any, **overrides: Any) -> TelegramFileService:
        options = dict(
            max_file_size_bytes=settings.input_limit_bytes,
            local_mode=settings.uses_local_bot_api,
            local_dir=settings.bot_api_local_dir,
            files_base_url=settings.bot_api_files_url,
        )
        options.update(overrides)
        return cls(bot, **options)

    @property
    def limit_bytes(self) -> int:
        return self._max_file_size_bytes

    def check_size(self, incoming: IncomingFile) -> None:
        if incoming.exceeds(self._max_file_size_bytes):
            raise FileTooLargeError(incoming.size or 0, self._max_file_size_bytes)

    async def fetch(
        self, incoming: IncomingFile, workspace: JobWorkspace, *, job_id: str | None = None
    ) -> FetchedFile:
        """Make ``incoming`` available as a local file.

        Downloads land in ``workspace``; a file already on this filesystem is
        borrowed without copying.
        """
        self.check_size(incoming)
        telegram_file = await self._get_file(incoming.file_id)

        declared = getattr(telegram_file, "file_size", None)
        if declared and declared > self._max_file_size_bytes:
            raise FileTooLargeError(declared, self._max_file_size_bytes)

        location = resolve_file_location(
            str(getattr(telegram_file, "file_path", "") or ""),
            local_mode=self._local_mode,
            local_dir=self._local_dir,
            files_base_url=self._files_base_url,
        )
        log_extra = {"job_id": job_id or "-"}

        if location.kind is SourceKind.LOCAL_PATH:
            path = Path(location.file_path)
            size = path.stat().st_size
            if size > self._max_file_size_bytes:
                raise FileTooLargeError(size, self._max_file_size_bytes)
            logger.info("using local Bot API file in place (%d bytes)", size, extra=log_extra)
            return FetchedFile(path=path, size=size, location=location)

        destination = workspace.new_file(incoming.extension, prefix="src_")
        if location.kind is SourceKind.LOCAL_HTTP:
            size = await stream_to_file(
                self._http.stream(location.url or "", chunk_size=self._chunk_size),
                destination,
                limit_bytes=self._max_file_size_bytes,
            )
        else:
            size = await self._download_cloud(location.file_path, destination, declared)

        logger.info(
            "fetched %d bytes as %s via %s",
            size,
            incoming.kind.value,
            location.kind.value,
            extra=log_extra,
        )
        return FetchedFile(path=destination, size=size, location=location)

    async def release(self, fetched: FetchedFile | None, *, job_id: str | None = None) -> None:
        """Free the Bot API server's copy once a job is finished with it.

        Best effort: the server's own TTL sweep is the backstop.
        """
        if fetched is None or fetched.location.kind is not SourceKind.LOCAL_HTTP:
            return
        try:
            await self._http.delete(fetched.location.url or "")
        except Exception:  # noqa: BLE001 - cleanup must never fail a job
            logger.warning(
                "could not release the Bot API server's copy", extra={"job_id": job_id or "-"}
            )

    async def _get_file(self, file_id: str) -> Any:
        try:
            return await self._bot.get_file(file_id)
        except Exception as exc:
            # The cloud API refuses getFile above 20 MB even when the size was
            # not declared up front.
            if "too big" in str(exc).lower():
                raise FileTooLargeError(0, self._max_file_size_bytes) from exc
            raise

    async def _download_cloud(self, file_path: str, destination: Path, declared: int | None) -> int:
        # aiogram streams this to disk in chunks; it is never buffered whole.
        await self._bot.download_file(
            file_path,
            destination=destination,
            timeout=transfer_timeout(declared),
            chunk_size=self._chunk_size,
        )
        size = destination.stat().st_size if destination.exists() else 0
        if size > self._max_file_size_bytes:
            destination.unlink(missing_ok=True)
            raise FileTooLargeError(size, self._max_file_size_bytes)
        return size
