"""Video -> Telegram video note (circle) conversion.

The FFmpeg invocation and the split plan are pure functions so they can be
unit tested without touching the filesystem; the service is a thin async
runner around them.

A long video is split into consecutive segments of at most the video-note
limit. Each segment is encoded straight from the source with an accurate
input seek, so the chunks tile the timeline exactly - no overlap, no gap - and
there is never one big intermediate encode on disk: a segment is produced,
sent and deleted before the next one starts.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
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

# Containers routinely report a few hundred milliseconds more than the real
# picture (audio padding, edit lists). A video within this much of a segment
# boundary is not split again just to produce a sliver of a circle.
SPLIT_TOLERANCE_SECONDS = 0.5


@dataclass(frozen=True)
class CircleSegment:
    """One circle's slice of the source timeline, in seconds."""

    index: int
    start: float
    duration: float

    @property
    def end(self) -> float:
        return self.start + self.duration


def effective_max_duration(max_duration: float) -> float:
    return float(min(max_duration, TELEGRAM_VIDEO_NOTE_MAX_DURATION))


def needs_split(duration: float | None, max_duration: float) -> bool:
    """Whether a video is too long for a single circle."""
    if not duration:
        return False
    return duration > effective_max_duration(max_duration) + SPLIT_TOLERANCE_SECONDS


def plan_circle_segments(duration: float, max_duration: float) -> list[CircleSegment]:
    """Tile ``[0, duration)`` with consecutive segments of ``max_duration``.

    Every segment but the last is exactly ``max_duration`` long; the last one
    takes whatever remains. An unknown duration yields a single capped circle,
    which is what the bot always did.
    """
    limit = effective_max_duration(max_duration)
    if limit <= 0:
        raise ValueError("max_duration must be positive")
    if not duration or duration <= 0:
        return [CircleSegment(index=0, start=0.0, duration=limit)]

    count = max(1, math.ceil((duration - SPLIT_TOLERANCE_SECONDS) / limit))
    segments = []
    for index in range(count):
        start = index * limit
        segments.append(
            CircleSegment(index=index, start=start, duration=min(limit, duration - start))
        )
    return segments


def build_circle_ffmpeg_args(
    ffmpeg_bin: str,
    source: Path,
    destination: Path,
    *,
    size: int,
    duration_limit: float,
    with_audio: bool = True,
    start: float = 0.0,
) -> list[str]:
    """Crop to a centred square, scale to ``size`` and encode a Telegram-safe MP4.

    FFmpeg auto-applies the display matrix while decoding, so cropping here
    operates on already-rotated frames and the output carries no rotation tag.

    ``start`` is an *input* seek: FFmpeg jumps to the nearest keyframe and then
    decodes and discards up to the exact timestamp, so consecutive segments
    meet frame-accurately without re-reading the whole file each time.
    """
    if size <= 0 or size % 2:
        raise ValueError("video note size must be a positive even number")
    if start < 0:
        raise ValueError("start must not be negative")

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
    ]
    if start > 0:
        args += ["-ss", f"{start:.3f}"]
    args += [
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


class CircleResult(ProcessedFile):
    """Marker subclass so callers can type against circle output."""


class VideoCircleService(Protocol):
    """Interface the circle router depends on."""

    async def probe(self, source: Path, *, job_id: str | None = None) -> MediaInfo: ...

    async def encode_segment(
        self,
        source: Path,
        destination: Path,
        segment: CircleSegment,
        *,
        with_audio: bool,
        job_id: str | None = None,
    ) -> CircleResult: ...


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
        self._max_duration = effective_max_duration(max_duration)
        self._timeout = timeout

    @property
    def max_duration(self) -> float:
        return self._max_duration

    async def probe(self, source: Path, *, job_id: str | None = None) -> MediaInfo:
        return await self._probe.probe(source, job_id=job_id)

    def plan(self, info: MediaInfo) -> list[CircleSegment]:
        return plan_circle_segments(info.duration, self._max_duration)

    async def encode_segment(
        self,
        source: Path,
        destination: Path,
        segment: CircleSegment,
        *,
        with_audio: bool,
        job_id: str | None = None,
    ) -> CircleResult:
        args = build_circle_ffmpeg_args(
            self._ffmpeg_bin,
            source,
            destination,
            size=self._size,
            duration_limit=segment.duration,
            with_audio=with_audio,
            start=segment.start,
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
            "circle %d ready side=%s start=%.1fs duration=%.1fs",
            segment.index + 1,
            self._size,
            segment.start,
            segment.duration,
            extra={"job_id": job_id or "-"},
        )
        return CircleResult(
            path=destination,
            size_bytes=destination.stat().st_size,
            filename=destination.name,
        )

    async def to_circle(
        self, source: Path, destination: Path, *, job_id: str | None = None
    ) -> CircleResult:
        """The first circle of ``source`` - the whole video when it is short."""
        info: MediaInfo = await self.probe(source, job_id=job_id)
        first = self.plan(info)[0]
        return await self.encode_segment(
            source, destination, first, with_audio=info.has_audio, job_id=job_id
        )
