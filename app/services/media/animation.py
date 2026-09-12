"""GIF and MP4 conversion.

Three jobs live here, and they all come down to the same two encoders:

* **video -> GIF.** A GIF has no inter-frame compression worth the name, so
  everything is about keeping the frame count and the frame size down: the
  rate is capped, the long side is capped, and anything longer than a few
  seconds is cut to a clip (the caller picks which one). The palette is built
  in a *separate first pass* rather than the usual ``split``/``palettegen``
  filter chain, because that chain buffers every frame of the clip in memory
  until the palette exists - exactly what a 1 GB container cannot afford.
* **GIF -> MP4.** H.264, silent, faststart, even dimensions. A GIF's stored
  rate is often nominal (100 fps with duplicated frames), so it is capped.
* **Optimize GIF.** The same palette pass, with fewer colours and, when the
  source is generous, a lower rate and a smaller frame. Whether the result is
  worth sending is decided on the real size, by the caller.

As elsewhere: planners and argument builders are pure functions, the service
is a thin async runner, every invocation states its thread budget, and every
output is probed before it is handed back.
"""

from __future__ import annotations

import enum
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from app.services.media.base import MediaProcessingError, ProcessingErrorCode
from app.services.media.optimizer import (
    MAX_VIDEO_PIXELS,
    RC_LOOKAHEAD,
    VIDEO_FORMATS,
    _display_size,
    _is_picture,
    _load,
    _number,
    classify_failure,
    encode_threads,
    parse_rate,
)
from app.services.media.probe import _stream_rotation, build_ffprobe_args
from app.utils.subprocess import (
    CommandFailed,
    CommandNotFound,
    CommandTimeout,
    run_command,
    run_command_streaming,
)
from app.utils.temp_files import safe_display_name

logger = logging.getLogger(__name__)

OUTPUT_PREFIX = "atreox_"

# ffprobe's name for an animated GIF, which the optimizer deliberately refuses.
GIF_FORMAT = "gif"
ACCEPTED_FORMATS = VIDEO_FORMATS | {GIF_FORMAT}

# --- GIF budget -------------------------------------------------------------
# A GIF frame is stored as an indexed bitmap, so its size grows with the frame
# area and the frame count and with little else. 720px at 15 fps for six
# seconds is around 90 frames - a few megabytes, which Telegram plays inline.
GIF_FRAME_RATE = 15.0
GIF_MAX_LONG_SIDE = 720
# Longer than this and a GIF stops being a GIF, so the caller picks a clip.
GIF_CLIP_SECONDS = 6.0
# Up to this, the whole thing is converted without asking.
CLIP_CHOICE_ABOVE_SECONDS = 15.0

# Re-encoding an existing GIF: fewer colours first, then rate, then size.
OPTIMIZE_COLOURS = 128
OPTIMIZE_FRAME_RATE = 12.0
OPTIMIZE_MAX_LONG_SIDE = 480

# MP4 out of a GIF: GIFs are small and flat, so this is cheap and looks clean.
MP4_CRF = 23
MP4_PRESET = "veryfast"
MP4_MAX_FRAME_RATE = 30.0

PALETTE_FILENAME = "palette.png"


class Conversion(str, enum.Enum):
    TO_GIF = "to_gif"
    TO_MP4 = "to_mp4"
    OPTIMIZE_GIF = "optimize_gif"


class ClipChoice(str, enum.Enum):
    FIRST = "first"
    MIDDLE = "middle"
    CUSTOM = "custom"


@dataclass(frozen=True)
class AnimationSource:
    """What the file is, in the terms this tool needs."""

    size_bytes: int
    width: int                 # as displayed: rotation and pixel aspect applied
    height: int
    duration: float
    frame_rate: float
    video_codec: str
    video_stream: int
    is_gif: bool
    has_audio: bool = False
    format_name: str = ""

    @property
    def long_side(self) -> int:
        return max(self.width, self.height)

    @property
    def needs_a_clip(self) -> bool:
        """Too long to turn into one GIF without asking which part."""
        return self.duration > CLIP_CHOICE_ABOVE_SECONDS

    @property
    def conversions(self) -> tuple[Conversion, ...]:
        if self.is_gif:
            return (Conversion.TO_MP4, Conversion.OPTIMIZE_GIF)
        return (Conversion.TO_GIF, Conversion.TO_MP4)


def parse_animation_source(payload: str, size_bytes: int) -> AnimationSource:
    """Classify a probed file for this tool, or refuse it."""
    data = _load(payload)
    fmt = data.get("format") or {}
    format_name = str(fmt.get("format_name") or "")
    names = {name.strip() for name in format_name.split(",") if name.strip()}
    if not names & ACCEPTED_FORMATS:
        raise MediaProcessingError(
            ProcessingErrorCode.UNSUPPORTED, f"input format {format_name or 'unknown'!r} refused"
        )

    streams: Sequence[dict[str, Any]] = data.get("streams") or []
    visual = [s for s in streams if s.get("codec_type") == "video" and not _is_picture(s)]
    if not visual:
        raise MediaProcessingError(ProcessingErrorCode.UNSUPPORTED, "no moving picture")
    video = visual[0]

    is_gif = GIF_FORMAT in names
    if is_gif:
        # A GIF's pixels are square by definition, but the demuxer still derives
        # a sample aspect ratio from the file's pixel-density fields (63:64 is
        # common), which would quietly shave a column off every conversion.
        width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    else:
        width, height = _display_size(video, _stream_rotation(video))
    if width <= 0 or height <= 0:
        raise MediaProcessingError(ProcessingErrorCode.PROBE_FAILED, "missing dimensions")
    if width * height > MAX_VIDEO_PIXELS:
        raise MediaProcessingError(
            ProcessingErrorCode.TOO_LARGE, f"{width}x{height} exceeds the video pixel budget"
        )

    return AnimationSource(
        size_bytes=size_bytes,
        width=width,
        height=height,
        duration=_number(fmt.get("duration")) or _number(video.get("duration")),
        frame_rate=parse_rate(video.get("avg_frame_rate")) or parse_rate(video.get("r_frame_rate")),
        video_codec=str(video.get("codec_name") or "unknown"),
        video_stream=int(video.get("index") or 0),
        is_gif=is_gif,
        has_audio=any(s.get("codec_type") == "audio" and s.get("codec_name") for s in streams),
        format_name=format_name,
    )


# --- clip selection ----------------------------------------------------------


@dataclass(frozen=True)
class Clip:
    start: float
    duration: float

    @property
    def end(self) -> float:
        return self.start + self.duration


def parse_timecode(raw: str) -> float | None:
    """``12``, ``1:05`` or ``1:05.5`` in seconds; ``None`` when it is not a time."""
    text = (raw or "").strip().replace(",", ".")
    if not text or len(text) > 12:
        return None
    parts = text.split(":")
    if len(parts) > 2:
        return None
    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        return None
    if any(number < 0 for number in numbers):
        return None
    if len(numbers) == 2:
        if numbers[1] >= 60:
            return None
        return numbers[0] * 60 + numbers[1]
    return numbers[0]


def plan_clip(
    source: AnimationSource,
    choice: ClipChoice = ClipChoice.FIRST,
    custom_start: float | None = None,
) -> Clip:
    """The piece of the source that becomes the GIF.

    A short source is taken whole. Otherwise the clip is
    :data:`GIF_CLIP_SECONDS` long, and never runs past the end of the file.
    """
    duration = source.duration or GIF_CLIP_SECONDS
    if duration <= GIF_CLIP_SECONDS:
        return Clip(0.0, duration)

    length = GIF_CLIP_SECONDS
    if choice is ClipChoice.MIDDLE:
        start = max(0.0, (duration - length) / 2)
    elif choice is ClipChoice.CUSTOM:
        if custom_start is None or custom_start >= duration:
            raise MediaProcessingError(
                ProcessingErrorCode.UNSUPPORTED, "start time is past the end of the video"
            )
        start = max(0.0, min(custom_start, max(0.0, duration - 0.1)))
    else:
        start = 0.0
    return Clip(round(start, 3), round(min(length, duration - start), 3))


# --- planning (pure) ---------------------------------------------------------


def _even(value: float) -> int:
    return max(2, int(value) // 2 * 2)


def fit_long_side(width: int, height: int, cap: int) -> tuple[int, int]:
    """Fit the long side under ``cap``; never upscale; keep the aspect ratio."""
    longest = max(width, height)
    scale = cap / longest if longest > cap else 1.0
    return _even(round(width * scale)), _even(round(height * scale))


@dataclass(frozen=True)
class GifPlan:
    width: int
    height: int
    frame_rate: float
    clip: Clip
    colours: int
    threads: int


def plan_gif(
    source: AnimationSource,
    clip: Clip,
    *,
    long_side: int = GIF_MAX_LONG_SIDE,
    frame_rate: float = GIF_FRAME_RATE,
    colours: int = 256,
    cpus: int | None = None,
) -> GifPlan:
    width, height = fit_long_side(source.width, source.height, long_side)
    rate = min(frame_rate, source.frame_rate) if source.frame_rate else frame_rate
    return GifPlan(
        width=width,
        height=height,
        # Never below a rate that still reads as motion.
        frame_rate=round(max(5.0, rate), 3),
        clip=clip,
        colours=max(16, min(256, colours)),
        threads=encode_threads(source.width, source.height, cpus),
    )


def plan_optimized_gif(source: AnimationSource, *, cpus: int | None = None) -> GifPlan:
    """Same animation, fewer colours, and a smaller frame only if it is large."""
    return plan_gif(
        source,
        Clip(0.0, source.duration),
        long_side=min(OPTIMIZE_MAX_LONG_SIDE, source.long_side),
        frame_rate=OPTIMIZE_FRAME_RATE,
        colours=OPTIMIZE_COLOURS,
        cpus=cpus,
    )


@dataclass(frozen=True)
class Mp4Plan:
    width: int
    height: int
    frame_rate_cap: float | None
    threads: int


def plan_mp4(source: AnimationSource, *, cpus: int | None = None) -> Mp4Plan:
    """The same picture in H.264: even dimensions, a sane rate, no sound."""
    return Mp4Plan(
        width=_even(source.width),
        height=_even(source.height),
        frame_rate_cap=(
            MP4_MAX_FRAME_RATE if source.frame_rate > MP4_MAX_FRAME_RATE + 0.5 else None
        ),
        threads=encode_threads(source.width, source.height, cpus),
    )


# --- argument builders (pure) -------------------------------------------------


def _clip_args(clip: Clip | None) -> list[str]:
    if clip is None:
        return []
    # Before -i, so the decoder seeks instead of decoding and discarding.
    args = ["-ss", f"{clip.start:.3f}"]
    if clip.duration > 0:
        args += ["-t", f"{clip.duration:.3f}"]
    return args


def _gif_scale(plan: GifPlan) -> str:
    return f"fps={plan.frame_rate:g},scale={plan.width}:{plan.height}:flags=lanczos"


def build_palette_args(ffmpeg_bin: str, source: Path, palette: Path, plan: GifPlan) -> list[str]:
    """First pass: the palette for this clip, as a one-frame PNG.

    Run separately from the encode on purpose - the single-command form buffers
    the whole clip in memory while the palette is computed.
    """
    threads = str(plan.threads)
    return [
        ffmpeg_bin, "-y", "-hide_banner", "-nostdin", "-loglevel", "error", "-nostats",
        "-threads", threads,
        *_clip_args(plan.clip),
        "-i", str(source),
        "-map", "0:v:0",
        "-an", "-sn", "-dn",
        "-filter_threads", threads,
        "-vf", f"{_gif_scale(plan)},palettegen=max_colors={plan.colours}:stats_mode=diff",
        "-threads", threads,
        "-frames:v", "1",
        "-f", "image2", "-update", "1",
        str(palette),
    ]


def build_gif_args(
    ffmpeg_bin: str, source: Path, palette: Path, destination: Path, plan: GifPlan
) -> list[str]:
    """Second pass: the GIF itself, drawn against the palette. No audio, loops."""
    threads = str(plan.threads)
    return [
        ffmpeg_bin, "-y", "-hide_banner", "-nostdin",
        "-loglevel", "error", "-nostats", "-progress", "pipe:1",
        "-threads", threads,
        *_clip_args(plan.clip),
        "-i", str(source),
        "-i", str(palette),
        "-an", "-sn", "-dn",
        "-map_metadata", "-1",
        "-filter_threads", threads,
        "-lavfi",
        f"[0:v]{_gif_scale(plan)}[x];"
        "[x][1:v]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle",
        "-threads", threads,
        # 0 means "for ever", which is what a GIF is expected to do.
        "-loop", "0",
        "-fflags", "+bitexact",
        "-f", "gif",
        str(destination),
    ]


def build_mp4_args(
    ffmpeg_bin: str, source: Path, destination: Path, src: AnimationSource, plan: Mp4Plan
) -> list[str]:
    """H.264 in a faststart MP4, silent, nothing carried over from the source."""
    threads = str(plan.threads)
    filters = [f"scale={plan.width}:{plan.height}:flags=lanczos", "setsar=1"]
    if plan.frame_rate_cap:
        filters.append(f"fps={plan.frame_rate_cap:g}")
    filters.append("format=yuv420p")
    return [
        ffmpeg_bin, "-y", "-hide_banner", "-nostdin",
        "-loglevel", "error", "-nostats", "-progress", "pipe:1",
        "-threads", threads,
        "-i", str(source),
        "-map", f"0:{src.video_stream}",
        # An animation has no soundtrack to keep, and a GIF never had one.
        "-an", "-sn", "-dn",
        "-map_metadata", "-1",
        "-map_chapters", "-1",
        "-filter_threads", threads,
        "-vf", ",".join(filters),
        "-c:v", "libx264",
        "-preset", MP4_PRESET,
        "-threads", threads,
        "-rc-lookahead", str(RC_LOOKAHEAD),
        "-profile:v", "main",
        "-pix_fmt", "yuv420p",
        "-crf", str(MP4_CRF),
        "-metadata:s:v", "rotate=0",
        "-movflags", "+faststart",
        "-fflags", "+bitexact",
        "-f", "mp4",
        str(destination),
    ]


# --- verification (pure) ------------------------------------------------------


def _fail(detail: str) -> None:
    raise MediaProcessingError(ProcessingErrorCode.VERIFY_FAILED, detail)


def verify_gif(output: dict[str, Any], plan: GifPlan) -> None:
    streams = [s for s in output.get("streams") or [] if s.get("codec_type") == "video"]
    if len(streams) != 1 or streams[0].get("codec_name") != "gif":
        _fail("the output is not a GIF")
    stream = streams[0]
    width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
    if (width, height) != (plan.width, plan.height):
        _fail(f"dimensions {width}x{height}, planned {plan.width}x{plan.height}")
    if any(s.get("codec_type") == "audio" for s in output.get("streams") or []):
        _fail("a GIF cannot carry audio")


def verify_mp4(output: dict[str, Any], source: AnimationSource, plan: Mp4Plan) -> None:
    fmt = output.get("format") or {}
    streams = output.get("streams") or []
    videos = [s for s in streams if s.get("codec_type") == "video"]
    if len(videos) != 1 or videos[0].get("codec_name") != "h264":
        _fail("expected exactly one h264 stream")
    video = videos[0]
    if (int(video.get("width") or 0), int(video.get("height") or 0)) != (plan.width, plan.height):
        _fail(f"dimensions {video.get('width')}x{video.get('height')}")
    if any(s.get("codec_type") == "audio" for s in streams):
        _fail("the animation gained a soundtrack")
    duration = float(fmt.get("duration") or 0)
    if source.duration and abs(duration - source.duration) > max(1.0, source.duration * 0.05):
        _fail(f"duration {duration:.2f}s, source {source.duration:.2f}s")


def converted_filename(original_filename: str | None, extension: str) -> str:
    """``atreox_<original name>.<ext>``, sanitised."""
    basename = (original_filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    stem = basename.rsplit(".", 1)[0] if "." in basename else basename
    stem = safe_display_name(stem, "animation")
    if not any(character.isalnum() for character in stem):
        stem = "animation"
    return f"{OUTPUT_PREFIX}{stem}{extension}"


# --- the service --------------------------------------------------------------


@dataclass(frozen=True)
class ConvertedMedia:
    path: Path
    size_bytes: int
    filename: str
    conversion: Conversion
    width: int
    height: int
    duration: float = 0.0

    @property
    def is_gif(self) -> bool:
        return self.conversion is not Conversion.TO_MP4


ProgressCallback = Callable[[float], Awaitable[None]]


class AnimationService:
    """GIF and MP4 conversion, one job at a time."""

    def __init__(
        self,
        *,
        ffmpeg_bin: str,
        ffprobe_bin: str,
        timeout: float,
        probe_timeout: float,
    ) -> None:
        self._ffmpeg_bin = ffmpeg_bin
        self._ffprobe_bin = ffprobe_bin
        self._timeout = timeout
        self._probe_timeout = probe_timeout

    async def analyze(self, source: Path, *, job_id: str | None = None) -> AnimationSource:
        analysis = parse_animation_source(
            await self._probe(source, job_id=job_id), source.stat().st_size
        )
        logger.info(
            "animation input %s %dx%d %.1fs gif=%s",
            analysis.video_codec, analysis.width, analysis.height, analysis.duration,
            analysis.is_gif, extra={"job_id": job_id or "-"},
        )
        return analysis

    async def to_gif(
        self,
        source: Path,
        workspace,
        analysis: AnimationSource,
        clip: Clip,
        *,
        job_id: str | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> ConvertedMedia:
        return await self._encode_gif(
            source, workspace, plan_gif(analysis, clip), Conversion.TO_GIF, job_id, on_progress
        )

    async def optimize_gif(
        self,
        source: Path,
        workspace,
        analysis: AnimationSource,
        *,
        job_id: str | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> ConvertedMedia:
        return await self._encode_gif(
            source, workspace, plan_optimized_gif(analysis), Conversion.OPTIMIZE_GIF,
            job_id, on_progress,
        )

    async def _encode_gif(self, source, workspace, plan, conversion, job_id, on_progress):
        palette = workspace.path / PALETTE_FILENAME
        logger.info(
            "gif encode %dx%d %.0ffps %.2fs colours=%d threads=%d",
            plan.width, plan.height, plan.frame_rate, plan.clip.duration, plan.colours,
            plan.threads, extra={"job_id": job_id or "-"},
        )
        await self._run(
            build_palette_args(self._ffmpeg_bin, source, palette, plan), job_id=job_id
        )
        if not palette.exists() or palette.stat().st_size == 0:
            raise MediaProcessingError(ProcessingErrorCode.ENCODE_FAILED, "no palette produced")

        destination = workspace.new_file(".gif", prefix="gif_")
        await self._run(
            build_gif_args(self._ffmpeg_bin, source, palette, destination, plan),
            job_id=job_id,
            duration=plan.clip.duration,
            on_progress=on_progress,
        )
        self._ensure_written(destination)
        verify_gif(json.loads(await self._probe(destination, job_id=job_id)), plan)
        size = destination.stat().st_size
        logger.info("gif ready (%d bytes)", size, extra={"job_id": job_id or "-"})
        return ConvertedMedia(
            path=destination, size_bytes=size, filename=destination.name,
            conversion=conversion, width=plan.width, height=plan.height,
            duration=plan.clip.duration,
        )

    async def to_mp4(
        self,
        source: Path,
        workspace,
        analysis: AnimationSource,
        *,
        job_id: str | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> ConvertedMedia:
        plan = plan_mp4(analysis)
        destination = workspace.new_file(".mp4", prefix="anim_")
        logger.info(
            "mp4 encode %dx%d threads=%d", plan.width, plan.height, plan.threads,
            extra={"job_id": job_id or "-"},
        )
        await self._run(
            build_mp4_args(self._ffmpeg_bin, source, destination, analysis, plan),
            job_id=job_id,
            duration=analysis.duration,
            on_progress=on_progress,
        )
        self._ensure_written(destination)
        verify_mp4(json.loads(await self._probe(destination, job_id=job_id)), analysis, plan)
        size = destination.stat().st_size
        logger.info("mp4 ready (%d bytes)", size, extra={"job_id": job_id or "-"})
        return ConvertedMedia(
            path=destination, size_bytes=size, filename=destination.name,
            conversion=Conversion.TO_MP4, width=plan.width, height=plan.height,
            duration=analysis.duration,
        )

    async def _run(self, args, *, job_id, duration: float = 0.0, on_progress=None) -> None:
        async def on_line(line: str) -> None:
            if on_progress is None or not duration or not line.startswith("out_time_us="):
                return
            try:
                position = max(0.0, float(line.partition("=")[2])) / 1_000_000
            except ValueError:
                return
            await on_progress(min(1.0, position / duration))

        try:
            if on_progress is None:
                await run_command(args, timeout=self._timeout, job_id=job_id)
            else:
                await run_command_streaming(
                    args, timeout=self._timeout, on_line=on_line, job_id=job_id
                )
        except CommandNotFound as exc:
            raise MediaProcessingError(ProcessingErrorCode.TOOL_MISSING, str(exc)) from exc
        except CommandTimeout as exc:
            raise MediaProcessingError(ProcessingErrorCode.TIMEOUT, str(exc)) from exc
        except CommandFailed as exc:
            raise MediaProcessingError(
                classify_failure(exc.stderr, exc.returncode), str(exc)
            ) from exc

    @staticmethod
    def _ensure_written(destination: Path) -> None:
        if not destination.exists() or destination.stat().st_size == 0:
            raise MediaProcessingError(ProcessingErrorCode.EMPTY_OUTPUT, "empty conversion output")

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
