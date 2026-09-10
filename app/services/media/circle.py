"""Video -> Telegram video note (circle) conversion.

The FFmpeg invocation is built by a pure function so it can be unit tested
without touching the filesystem; the service is a thin async runner around it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

from app.services.media.base import (
    MediaInfo,
    MediaProcessingError,
    ProcessedFile,
    ProcessingErrorCode,
)
from app.services.media.probe import MediaProbe
from app.utils.subprocess import (
    CommandFailed,
    CommandNotFound,
    CommandTimeout,
    run_command,
)

logger = logging.getLogger(__name__)

# Telegram will not accept a video note longer than this.
TELEGRAM_VIDEO_NOTE_MAX_DURATION = 60


def build_circle_ffmpeg_args(
    ffmpeg_bin: str,
    source: Path,
    destination: Path,
    *,
    size: int,
    duration_limit: float,
    with_audio: bool = True,
) -> list[str]:
    """Crop to a centred square, scale to ``size`` and encode a Telegram-safe MP4.

    FFmpeg auto-applies the display matrix while decoding, so cropping here
    operates on already-rotated frames and the output carries no rotation tag.
    """
    if size <= 0 or size % 2:
        raise ValueError("video note size must be a positive even number")

    video_filter = (
        "crop='min(iw,ih)':'min(iw,ih)'"
        f",scale={size}:{size}:flags=lanczos"
        ",setsar=1"
        ",fps=30"
    )

    args = [
        ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-i", str(source),
        "-t", f"{min(duration_limit, TELEGRAM_VIDEO_NOTE_MAX_DURATION):.3f}",
        "-vf", video_filter,
        "-c:v", "libx264",
        "-profile:v", "main",
        "-level", "3.1",
        "-pix_fmt", "yuv420p",
        "-preset", "veryfast",
        "-crf", "26",
        "-movflags", "+faststart",
        "-metadata:s:v", "rotate=0",
    ]
    if with_audio:
        args += ["-c:a", "aac", "-b:a", "96k", "-ac", "1", "-ar", "44100"]
    else:
        args += ["-an"]
    args += [str(destination)]
    return args


class VideoCircleService(Protocol):
    """Interface the circle router depends on."""

    async def to_circle(
        self, source: Path, destination: Path, *, job_id: str | None = None
    ) -> "CircleResult": ...


class CircleResult(ProcessedFile):
    """Marker subclass so callers can type against circle output."""


class FfmpegVideoCircleService:
    """ffprobe + ffmpeg implementation of :class:`VideoCircleService`."""

    def __init__(
        self,
        *,
        ffmpeg_bin: str,
        probe: MediaProbe,
        size: int,
        max_duration: int,
        timeout: float,
    ) -> None:
        self._ffmpeg_bin = ffmpeg_bin
        self._probe = probe
        self._size = size
        self._max_duration = min(max_duration, TELEGRAM_VIDEO_NOTE_MAX_DURATION)
        self._timeout = timeout

    async def to_circle(
        self, source: Path, destination: Path, *, job_id: str | None = None
    ) -> CircleResult:
        info: MediaInfo = await self._probe.probe(source, job_id=job_id)
        duration_limit = min(self._max_duration, info.duration or self._max_duration)

        args = build_circle_ffmpeg_args(
            self._ffmpeg_bin,
            source,
            destination,
            size=self._size,
            duration_limit=duration_limit,
            with_audio=info.has_audio,
        )
        try:
            await run_command(args, timeout=self._timeout, job_id=job_id)
        except CommandNotFound as exc:
            raise MediaProcessingError(ProcessingErrorCode.TOOL_MISSING, str(exc)) from exc
        except CommandTimeout as exc:
            raise MediaProcessingError(ProcessingErrorCode.TIMEOUT, str(exc)) from exc
        except CommandFailed as exc:
            raise MediaProcessingError(ProcessingErrorCode.ENCODE_FAILED, str(exc)) from exc

        if not destination.exists() or destination.stat().st_size == 0:
            raise MediaProcessingError(ProcessingErrorCode.EMPTY_OUTPUT, "empty circle output")

        logger.info(
            "circle ready side=%s duration=%.1fs",
            self._size,
            duration_limit,
            extra={"job_id": job_id or "-"},
        )
        return CircleResult(
            path=destination,
            size_bytes=destination.stat().st_size,
            filename=destination.name,
        )
