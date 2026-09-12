"""Extract one still frame from a video.

The promise is that the still is exactly the picture the viewer sees at that
moment: the display matrix is applied while decoding (so a phone video taken
sideways comes out upright), the frame keeps its own dimensions - nothing is
scaled, cropped or padded - and no metadata from the source is carried over.

Seeking happens *before* the input, so FFmpeg jumps to the keyframe and decodes
forward from there instead of decoding the whole file and throwing it away;
one frame is decoded, one frame is written.
"""

from __future__ import annotations

import enum
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.media.base import MediaProcessingError, ProcessingErrorCode
from app.services.media.optimizer import (
    IMAGE_EXTENSIONS,
    VideoAnalysis,
    classify_failure,
    encode_threads,
    parse_analysis,
)
from app.services.media.probe import build_ffprobe_args
from app.utils.subprocess import (
    CommandFailed,
    CommandNotFound,
    CommandTimeout,
    run_command,
)
from app.utils.temp_files import safe_display_name

logger = logging.getLogger(__name__)

OUTPUT_PREFIX = "atreox_frame_"

# A still is JPEG or PNG; a frame is never written as WEBP.
FORMATS = ("jpeg", "png")
# Visually lossless for a single still (2 is FFmpeg's best practical setting).
JPEG_QUALITY = 2

# The last moments of a file are where a seek most often lands past the end;
# stepping back this far still gives the frame the user asked for.
_END_MARGIN = 0.05


class Position(str, enum.Enum):
    FIRST = "first"
    QUARTER = "quarter"
    MIDDLE = "middle"
    THREE_QUARTER = "three_quarter"
    CUSTOM = "custom"


_SHARES: dict[Position, float] = {
    Position.FIRST: 0.0,
    Position.QUARTER: 0.25,
    Position.MIDDLE: 0.5,
    Position.THREE_QUARTER: 0.75,
}


def seek_seconds(
    position: Position, duration: float, custom: float | None = None
) -> float:
    """Where in the file the frame is taken from, in seconds."""
    if position is Position.CUSTOM:
        if custom is None or custom < 0 or (duration and custom > duration):
            raise MediaProcessingError(
                ProcessingErrorCode.UNSUPPORTED, "the time is outside the video"
            )
        target = custom
    else:
        target = duration * _SHARES[position]
    if duration:
        # Never seek to the very last instant: there may be no frame there.
        target = min(target, max(0.0, duration - _END_MARGIN))
    return round(max(0.0, target), 3)


@dataclass(frozen=True)
class FramePlan:
    seconds: float
    image_format: str        # jpeg | png
    width: int
    height: int
    threads: int


def plan_frame(
    analysis: VideoAnalysis,
    position: Position,
    image_format: str,
    *,
    custom: float | None = None,
    cpus: int | None = None,
) -> FramePlan:
    if image_format not in FORMATS:
        raise MediaProcessingError(ProcessingErrorCode.UNSUPPORTED, f"{image_format} still")
    return FramePlan(
        seconds=seek_seconds(position, analysis.duration, custom),
        image_format=image_format,
        # The analysis already reports the displayed size, which is what
        # FFmpeg writes once it has applied the rotation.
        width=analysis.width,
        height=analysis.height,
        threads=encode_threads(analysis.width, analysis.height, cpus),
    )


def build_frame_args(
    ffmpeg_bin: str, source: Path, destination: Path, analysis: VideoAnalysis, plan: FramePlan
) -> list[str]:
    """Decode one frame at ``plan.seconds`` and write it, unscaled."""
    threads = str(plan.threads)
    args = [
        ffmpeg_bin, "-y", "-hide_banner", "-nostdin", "-loglevel", "error", "-nostats",
        "-threads", threads,
        # Before -i: seek by keyframe, then decode forward - not the whole file.
        "-ss", f"{plan.seconds:.3f}",
        "-i", str(source),
        "-map", f"0:{analysis.video_stream}",
        "-frames:v", "1",
        "-an", "-sn", "-dn",
        "-map_metadata", "-1",
        "-map_chapters", "-1",
        "-filter_threads", threads,
        "-threads", threads,
    ]
    if plan.image_format == "png":
        args += ["-c:v", "png", "-pred", "mixed", "-compression_level", "9"]
    else:
        args += ["-c:v", "mjpeg", "-q:v", str(JPEG_QUALITY), "-pix_fmt", "yuvj444p"]
    args += ["-fflags", "+bitexact", "-f", "image2", "-update", "1", str(destination)]
    return args


def verify_frame(output: dict[str, Any], plan: FramePlan) -> None:
    """One still, the size of the frame it came from."""
    streams = [s for s in output.get("streams") or [] if s.get("codec_type") == "video"]
    if len(streams) != 1:
        _fail("expected one still image")
    stream = streams[0]
    expected_codec = "png" if plan.image_format == "png" else "mjpeg"
    if str(stream.get("codec_name")) != expected_codec:
        _fail(f"the still was written as {stream.get('codec_name')!r}")
    width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
    if (width, height) != (plan.width, plan.height):
        _fail(f"dimensions {width}x{height}, frame {plan.width}x{plan.height}")


def _fail(detail: str) -> None:
    raise MediaProcessingError(ProcessingErrorCode.VERIFY_FAILED, detail)


def frame_filename(original_filename: str | None, image_format: str) -> str:
    """``atreox_frame_<original name>.<jpg|png>``, sanitised."""
    extension = IMAGE_EXTENSIONS.get(image_format, ".jpg")
    basename = (original_filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    stem = basename.rsplit(".", 1)[0] if "." in basename else basename
    stem = safe_display_name(stem, "video")
    if not any(character.isalnum() for character in stem):
        stem = "video"
    return f"{OUTPUT_PREFIX}{stem}{extension}"


@dataclass(frozen=True)
class ExtractedFrame:
    path: Path
    size_bytes: int
    filename: str
    width: int
    height: int
    seconds: float
    image_format: str


class FrameService:
    def __init__(
        self, *, ffmpeg_bin: str, ffprobe_bin: str, timeout: float, probe_timeout: float
    ) -> None:
        self._ffmpeg_bin = ffmpeg_bin
        self._ffprobe_bin = ffprobe_bin
        self._timeout = timeout
        self._probe_timeout = probe_timeout

    async def analyze(self, source: Path, *, job_id: str | None = None) -> VideoAnalysis:
        """The shared analysis, narrowed to what this tool can work on."""
        analysis = parse_analysis(
            await self._probe(source, job_id=job_id), source.stat().st_size
        )
        if not isinstance(analysis, VideoAnalysis):
            raise MediaProcessingError(
                ProcessingErrorCode.UNSUPPORTED, "a still picture has no frames to extract"
            )
        logger.info(
            "frame source %dx%d %.2fs", analysis.width, analysis.height, analysis.duration,
            extra={"job_id": job_id or "-"},
        )
        return analysis

    async def extract(
        self,
        source: Path,
        workspace,
        analysis: VideoAnalysis,
        position: Position,
        image_format: str,
        *,
        custom: float | None = None,
        job_id: str | None = None,
    ) -> ExtractedFrame:
        plan = plan_frame(analysis, position, image_format, custom=custom)
        destination = workspace.new_file(IMAGE_EXTENSIONS[plan.image_format], prefix="frame_")
        args = build_frame_args(self._ffmpeg_bin, source, destination, analysis, plan)
        logger.info(
            "frame at %.2fs as %s (%dx%d)", plan.seconds, plan.image_format,
            plan.width, plan.height, extra={"job_id": job_id or "-"},
        )
        try:
            await run_command(args, timeout=self._timeout, job_id=job_id)
        except CommandNotFound as exc:
            raise MediaProcessingError(ProcessingErrorCode.TOOL_MISSING, str(exc)) from exc
        except CommandTimeout as exc:
            raise MediaProcessingError(ProcessingErrorCode.TIMEOUT, str(exc)) from exc
        except CommandFailed as exc:
            raise MediaProcessingError(
                classify_failure(exc.stderr, exc.returncode), str(exc)
            ) from exc

        if not destination.exists() or destination.stat().st_size == 0:
            # A seek that lands in a gap produces no frame at all.
            raise MediaProcessingError(
                ProcessingErrorCode.EMPTY_OUTPUT, "no frame at that moment"
            )
        verify_frame(json.loads(await self._probe(destination, job_id=job_id)), plan)
        size = destination.stat().st_size
        logger.info("frame ready (%d bytes)", size, extra={"job_id": job_id or "-"})
        return ExtractedFrame(
            path=destination, size_bytes=size, filename=destination.name,
            width=plan.width, height=plan.height, seconds=plan.seconds,
            image_format=plan.image_format,
        )

    async def _probe(self, target: Path, *, job_id: str | None) -> str:
        try:
            result = await run_command(
                build_ffprobe_args(self._ffprobe_bin, target),
                timeout=self._probe_timeout,
                job_id=job_id,
            )
        except CommandNotFound as exc:
            raise MediaProcessingError(ProcessingErrorCode.TOOL_MISSING, str(exc)) from exc
        except CommandTimeout as exc:
            raise MediaProcessingError(ProcessingErrorCode.TIMEOUT, str(exc)) from exc
        except CommandFailed as exc:
            raise MediaProcessingError(ProcessingErrorCode.PROBE_FAILED, str(exc)) from exc
        return result.stdout
