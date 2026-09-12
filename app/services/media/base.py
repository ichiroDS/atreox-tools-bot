"""Shared media-processing types."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path


# The bot runs in a small container (Railway: 1 GB, 2 vCPU). FFmpeg sizes its
# thread pools from the CPUs it can *see* - the host's, not the container's
# quota - and every decoder and encoder thread holds frames of its own, so
# every invocation states its own budget. Measured peak RSS on a 2160x3840
# 60 fps source: circle 524 -> 124 MB, optimizer 1662 -> 345 MB.
MAX_ENCODE_THREADS = 2


class ProcessingErrorCode(str, enum.Enum):
    """Stable codes stored on ``jobs.error_code`` - never shown to users."""

    UNSUPPORTED = "unsupported_file"
    TOO_LARGE = "file_too_large"
    OUTPUT_TOO_LARGE = "output_too_large"
    DOWNLOAD_FAILED = "download_failed"
    PROBE_FAILED = "probe_failed"
    TIMEOUT = "processing_timeout"
    TOOL_MISSING = "tool_missing"
    ENCODE_FAILED = "encode_failed"
    METADATA_FAILED = "metadata_failed"
    VERIFY_FAILED = "verify_failed"
    EMPTY_OUTPUT = "empty_output"
    NO_AUDIO = "no_audio"
    # The file looked like media, but its audio cannot be decoded.
    CORRUPT_INPUT = "corrupt_input"
    SEND_FAILED = "send_failed"
    # The recipient's privacy settings refuse voice messages from this bot.
    VOICE_FORBIDDEN = "voice_forbidden"
    # The workspace filesystem ran out of room mid-job.
    DISK_FULL = "disk_full"
    # A tool was killed from outside (SIGKILL) - in a container, the OOM killer.
    PROCESS_KILLED = "process_killed"
    UNKNOWN = "unknown_error"


class MediaProcessingError(RuntimeError):
    """Raised by media services; handlers map this to a friendly message."""

    def __init__(self, code: ProcessingErrorCode, detail: str = "") -> None:
        super().__init__(detail or code.value)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class MediaInfo:
    """What ffprobe told us about an input file."""

    width: int
    height: int
    duration: float
    rotation: int = 0
    video_codec: str | None = None
    audio_codec: str | None = None

    @property
    def has_audio(self) -> bool:
        return self.audio_codec is not None

    @property
    def display_width(self) -> int:
        return self.height if self.rotation in (90, 270) else self.width

    @property
    def display_height(self) -> int:
        return self.width if self.rotation in (90, 270) else self.height

    @property
    def square_side(self) -> int:
        return min(self.display_width, self.display_height)


@dataclass(frozen=True)
class ProcessedFile:
    """A file produced inside a job workspace, ready to be sent."""

    path: Path
    size_bytes: int
    filename: str
