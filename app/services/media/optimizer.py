"""Media Optimizer: smaller photos and videos that still suit Telegram.

Three presets - Small, Balanced, High Quality - are *adaptive*: a video's
target bitrate follows its output resolution and frame rate, and is capped by
what the source itself spends, so a preset never asks for more bits than the
file already has. Encoding is bitrate-targeted (one-pass ABR under a VBV cap),
which is what makes the size estimate shown before processing honest.

Photos keep their format and exact pixel dimensions. JPEG and WEBP are
re-encoded at a preset quality (never above the source's), PNG is optimised
losslessly and transparency always survives. Images are encoded by a Pillow
worker in its own process (see ``image_worker.py``); videos by FFmpeg.

Every input is probed before (to plan) and every output after (to verify
dimensions, duration, codecs and that it opens). Nothing here decides whether
a result is worth sending - that is the caller's call, made on real sizes.

As elsewhere, argument builders and planners are pure functions; the service
is a thin async runner around them.
"""

from __future__ import annotations

import enum
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from app.services.media.base import MediaProcessingError, ProcessingErrorCode
from app.services.media.probe import _stream_rotation, build_ffprobe_args
from app.services.media.voice import classify_encode_failure
from app.utils.subprocess import (
    CommandFailed,
    CommandNotFound,
    CommandTimeout,
    run_command,
    run_command_streaming,
)
from app.utils.temp_files import JobWorkspace, safe_display_name

logger = logging.getLogger(__name__)

IMAGE_WORKER = Path(__file__).with_name("image_worker.py")

OUTPUT_PREFIX = "atreox_optimized_"

# Demuxers allowed on user input, as ffprobe names them. Playlists, text and
# image sequences are refused before anything is decoded.
VIDEO_FORMATS = frozenset(
    {"mov", "mp4", "m4a", "3gp", "3g2", "mj2", "matroska", "webm", "avi", "flv",
     "mpegts", "mpeg", "asf"}
)
IMAGE_FORMATS = frozenset({"image2", "jpeg_pipe", "png_pipe", "webp_pipe"})
IMAGE_CODECS = {"mjpeg": "jpeg", "png": "png", "webp": "webp"}
IMAGE_EXTENSIONS = {"jpeg": ".jpg", "png": ".png", "webp": ".webp"}

# Pixel formats that carry an alpha channel (pal8 may, through tRNS).
ALPHA_PIX_FMTS = frozenset(
    {"rgba", "bgra", "argb", "abgr", "ya8", "ya16be", "ya16le", "rgba64be", "rgba64le",
     "yuva420p", "yuva444p", "pal8"}
)
# 16-bit colour PNGs: Pillow would read them as 8-bit, so FFmpeg keeps them lossless.
_DEEP_PNG_PIX_FMTS = frozenset({"rgb48be", "rgb48le", "rgba64be", "rgba64le", "ya16be", "ya16le"})

# Frame rates above this are slow-motion captures; they are brought down to it.
MAX_FRAME_RATE = 60.0

# --- memory budget ------------------------------------------------------------
# The bot runs in a small container (Railway: 1 GB, 2 vCPU). FFmpeg sizes its
# thread pools from the CPUs it can *see* - the host's, not the container's
# quota - and every decoder and encoder thread holds frames of its own. Left
# automatic, a 2160x3840 60 fps source peaked at 1-1.7 GB and the kernel killed
# the encode (rc -9). So threads and x264's lookahead are explicit, sized from
# measurements of ffmpeg's peak RSS on exactly that source:
#   automatic threads            967-1662 MB
#   2 threads, lookahead 10       ~340 MB (High), ~220 MB (Small)
# which keeps two concurrent heavy jobs plus the bot itself under 1 GB.
MAX_ENCODE_THREADS = 2
# Output frames x264 buffers for rate control; the "fast" preset's default of
# 30 alone cost ~140 MB at 1080p.
RC_LOOKAHEAD = 10
# Past 4K a decoded frame is 50 MB+: one decode thread keeps 8K at ~450 MB
# (two threads: ~600 MB).
_SINGLE_THREAD_ABOVE_PIXELS = 4096 * 2304
# Largest video frame accepted at all (8K DCI).
MAX_VIDEO_PIXELS = 8192 * 4320
# Largest photo per format, from the encoder's measured peak memory, each kept
# near 400 MB or less:
#   JPEG  ~4 B/px (baseline past 24 MP)   50 MP -> ~210 MB
#   PNG   ~5 B/px                         50 MP -> ~250 MB
#   WEBP  ~24 B/px (libwebp's buffers)    16 MP -> ~390 MB
# Kept equal to image_worker.MAX_PIXELS, which enforces them again.
IMAGE_PIXEL_LIMITS = {"jpeg": 50_000_000, "png": 50_000_000, "webp": 16_000_000}
# 16-bit PNG goes through FFmpeg at ~26 B/px: 12 MP -> ~315 MB.
DEEP_PNG_PIXEL_LIMIT = 12_000_000
# MP4 box overhead, roughly, on top of the audio and video payload.
_CONTAINER_OVERHEAD = 1.015
# A preset whose estimate is not at least this much smaller than the source
# is not offered: it would spend an encode to save next to nothing.
_WORTHWHILE_RATIO = 0.95

# An output must be at least this much smaller than its source to be sent:
# a file that is the same size (or larger) is never a replacement worth having.
MIN_SAVING_RATIO = 0.02

_NO_SPACE_MARKERS = ("no space left on device", "disk full")


def is_worth_sending(before_bytes: int, after_bytes: int) -> bool:
    return after_bytes < before_bytes * (1 - MIN_SAVING_RATIO)


class MediaKind(str, enum.Enum):
    VIDEO = "video"
    IMAGE = "image"


class Preset(str, enum.Enum):
    SMALL = "small"
    BALANCED = "balanced"
    HIGH = "high"


@dataclass(frozen=True)
class PresetProfile:
    # Longest the *short* side may be: 720 means 1280x720 or 720x1280.
    max_short_side: int
    # Video bits per pixel per frame (at 30 fps) this preset aims for...
    bits_per_pixel: float
    # ...but never more than this share of what the source spends.
    source_share: float
    # Below this the picture falls apart: the source is already efficient.
    floor_bits_per_pixel: float
    audio_bitrate: int
    x264_preset: str


PROFILES: dict[Preset, PresetProfile] = {
    Preset.SMALL: PresetProfile(720, 0.050, 0.60, 0.020, 96_000, "veryfast"),
    Preset.BALANCED: PresetProfile(1080, 0.065, 0.75, 0.024, 128_000, "faster"),
    Preset.HIGH: PresetProfile(1080, 0.095, 0.90, 0.028, 160_000, "fast"),
}


# --- analysis ---------------------------------------------------------------


@dataclass(frozen=True)
class VideoAnalysis:
    size_bytes: int
    width: int              # as displayed: rotation and pixel aspect applied
    height: int
    duration: float
    video_codec: str
    video_stream: int
    frame_rate: float       # 0 when unknown
    video_bitrate: int      # bits/s; derived from the file size when not declared
    bitrate_declared: bool
    rotation: int = 0
    audio_codec: str | None = None
    audio_stream: int | None = None
    audio_bitrate: int | None = None
    audio_channels: int | None = None
    format_name: str = ""
    kind: MediaKind = MediaKind.VIDEO

    @property
    def has_audio(self) -> bool:
        return self.audio_codec is not None


@dataclass(frozen=True)
class ImageAnalysis:
    size_bytes: int
    width: int
    height: int
    format: str             # jpeg | png | webp
    pix_fmt: str = ""
    kind: MediaKind = MediaKind.IMAGE

    @property
    def has_alpha(self) -> bool:
        return self.pix_fmt in ALPHA_PIX_FMTS

    @property
    def deep_png(self) -> bool:
        return self.format == "png" and self.pix_fmt in _DEEP_PNG_PIX_FMTS


Analysis = VideoAnalysis | ImageAnalysis


def _load(payload: str) -> dict[str, Any]:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise MediaProcessingError(ProcessingErrorCode.PROBE_FAILED, "invalid ffprobe json") from exc
    return data if isinstance(data, dict) else {}


def _number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0


def parse_rate(raw: Any) -> float:
    """``30000/1001`` -> 29.97; ``0/0`` or junk -> 0."""
    if not raw or not isinstance(raw, str):
        return _number(raw)
    if "/" in raw:
        numerator, _, denominator = raw.partition("/")
        num, den = _number(numerator), _number(denominator)
        return num / den if num and den else 0.0
    return _number(raw)


def _names(format_name: str) -> set[str]:
    return {name.strip() for name in format_name.split(",") if name.strip()}


def _is_picture(stream: dict[str, Any]) -> bool:
    return bool((stream.get("disposition") or {}).get("attached_pic"))


def parse_analysis(payload: str, size_bytes: int) -> Analysis:
    """Classify a probed file as a photo or a video, or refuse it."""
    data = _load(payload)
    fmt = data.get("format") or {}
    format_name = str(fmt.get("format_name") or "")
    names = _names(format_name)
    streams: Sequence[dict[str, Any]] = data.get("streams") or []
    visual = [s for s in streams if s.get("codec_type") == "video" and not _is_picture(s)]

    if names & IMAGE_FORMATS:
        return _image_analysis(visual, size_bytes)
    if not names & VIDEO_FORMATS:
        raise MediaProcessingError(
            ProcessingErrorCode.UNSUPPORTED, f"input format {format_name or 'unknown'!r} refused"
        )
    if not visual:
        raise MediaProcessingError(ProcessingErrorCode.UNSUPPORTED, "no video stream")
    return _video_analysis(visual[0], streams, fmt, format_name, size_bytes)


def _image_analysis(visual: Sequence[dict[str, Any]], size_bytes: int) -> ImageAnalysis:
    if not visual:
        raise MediaProcessingError(ProcessingErrorCode.PROBE_FAILED, "image without a picture")
    stream = visual[0]
    image_format = IMAGE_CODECS.get(str(stream.get("codec_name")))
    if image_format is None:
        raise MediaProcessingError(
            ProcessingErrorCode.UNSUPPORTED, f"image codec {stream.get('codec_name')!r} refused"
        )
    width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
    if width <= 0 or height <= 0:
        raise MediaProcessingError(ProcessingErrorCode.PROBE_FAILED, "missing dimensions")
    analysis = ImageAnalysis(
        size_bytes=size_bytes, width=width, height=height, format=image_format,
        pix_fmt=str(stream.get("pix_fmt") or ""),
    )
    limit = DEEP_PNG_PIXEL_LIMIT if analysis.deep_png else IMAGE_PIXEL_LIMITS[image_format]
    if width * height > limit:
        raise MediaProcessingError(
            ProcessingErrorCode.TOO_LARGE, f"{width}x{height} exceeds the {image_format} pixel budget"
        )
    return analysis


def _display_size(stream: dict[str, Any], rotation: int) -> tuple[int, int]:
    width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
    sar = parse_rate(str(stream.get("sample_aspect_ratio") or "").replace(":", "/"))
    if sar and abs(sar - 1) > 0.01:
        width = round(width * sar)
    if rotation in (90, 270):
        width, height = height, width
    return width, height


def _video_analysis(
    video: dict[str, Any],
    streams: Sequence[dict[str, Any]],
    fmt: dict[str, Any],
    format_name: str,
    size_bytes: int,
) -> VideoAnalysis:
    rotation = _stream_rotation(video)
    width, height = _display_size(video, rotation)
    if width <= 0 or height <= 0:
        raise MediaProcessingError(ProcessingErrorCode.PROBE_FAILED, "missing dimensions")
    if width * height > MAX_VIDEO_PIXELS:
        raise MediaProcessingError(
            ProcessingErrorCode.TOO_LARGE, f"{width}x{height} exceeds the video pixel budget"
        )

    duration = _number(fmt.get("duration")) or _number(video.get("duration"))
    audio = next((s for s in streams if s.get("codec_type") == "audio" and s.get("codec_name")), None)
    audio_bitrate = int(_number(audio.get("bit_rate"))) if audio else None

    declared = int(_number(video.get("bit_rate")))
    if declared:
        video_bitrate = declared
    elif duration:
        # Not every container declares a per-stream rate (MKV, WebM). The file
        # size over its duration, less the audio, is a sound upper bound.
        total = int(_number(fmt.get("bit_rate"))) or int(size_bytes * 8 / duration)
        video_bitrate = max(0, total - (audio_bitrate or (128_000 if audio else 0)))
    else:
        video_bitrate = 0

    return VideoAnalysis(
        size_bytes=size_bytes,
        width=width,
        height=height,
        duration=duration,
        video_codec=str(video.get("codec_name") or "unknown"),
        video_stream=int(video.get("index") or 0),
        frame_rate=parse_rate(video.get("avg_frame_rate")) or parse_rate(video.get("r_frame_rate")),
        video_bitrate=video_bitrate,
        bitrate_declared=bool(declared),
        rotation=rotation,
        audio_codec=str(audio["codec_name"]) if audio else None,
        audio_stream=int(audio.get("index") or 0) if audio else None,
        audio_bitrate=audio_bitrate or None,
        audio_channels=int(_number(audio.get("channels"))) or None if audio else None,
        format_name=format_name,
    )


# --- video planning (pure) --------------------------------------------------


@dataclass(frozen=True)
class AudioPlan:
    copy: bool
    bitrate: int            # what it will cost, for the estimate
    downmix: bool = False   # more than two channels -> stereo


@dataclass(frozen=True)
class VideoPlan:
    preset: Preset
    width: int
    height: int
    frame_rate_cap: float | None
    video_bitrate: int
    maxrate: int
    bufsize: int
    audio: AudioPlan | None
    estimated_bytes: int | None
    worthwhile: bool
    # Decoder, filter and encoder threads - see "memory budget" above.
    threads: int = 1


def encode_threads(width: int, height: int, cpus: int | None = None) -> int:
    """Threads for one encode: never more than the budget, one past 4K."""
    if width * height > _SINGLE_THREAD_ABOVE_PIXELS:
        return 1
    return max(1, min(cpus or os.cpu_count() or 1, MAX_ENCODE_THREADS))


def _even(value: float) -> int:
    return max(2, int(value) // 2 * 2)


def output_size(width: int, height: int, max_short_side: int) -> tuple[int, int]:
    """Fit the short side under ``max_short_side``; never upscale; keep the
    aspect ratio; even dimensions (yuv420p needs them)."""
    short = min(width, height)
    scale = max_short_side / short if short > max_short_side else 1.0
    return _even(round(width * scale)), _even(round(height * scale))


def _rate_factor(frame_rate: float) -> float:
    """Bits scale with frame rate, but far from linearly: 60 fps costs ~1.5x."""
    fps = min(frame_rate or 30.0, MAX_FRAME_RATE)
    return (min(fps, 30.0) + max(0.0, fps - 30.0) * 0.5) / 30.0


def plan_audio(analysis: VideoAnalysis, profile: PresetProfile) -> AudioPlan | None:
    if not analysis.has_audio:
        return None
    channels = analysis.audio_channels or 2
    source_rate = analysis.audio_bitrate
    if (
        analysis.audio_codec == "aac"
        and source_rate
        and source_rate <= profile.audio_bitrate * 1.1
        and channels <= 2
    ):
        # Already lean AAC: copying costs nothing and loses nothing.
        return AudioPlan(copy=True, bitrate=source_rate)
    bitrate = profile.audio_bitrate
    if source_rate:
        bitrate = min(bitrate, max(64_000, source_rate))
    return AudioPlan(copy=False, bitrate=bitrate, downmix=channels > 2)


def plan_video(analysis: VideoAnalysis, preset: Preset, *, cpus: int | None = None) -> VideoPlan:
    profile = PROFILES[preset]
    width, height = output_size(analysis.width, analysis.height, profile.max_short_side)
    pixel_rate = width * height * 30 * _rate_factor(analysis.frame_rate)

    target = pixel_rate * profile.bits_per_pixel
    if analysis.video_bitrate:
        target = min(target, analysis.video_bitrate * profile.source_share)
    floor = pixel_rate * profile.floor_bits_per_pixel
    worthwhile = target >= floor
    target = int(max(target, floor))

    audio = plan_audio(analysis, profile)
    estimated = None
    if analysis.duration:
        payload = (target + (audio.bitrate if audio else 0)) * analysis.duration / 8
        estimated = int(payload * _CONTAINER_OVERHEAD)
        worthwhile = worthwhile and estimated < analysis.size_bytes * _WORTHWHILE_RATIO

    return VideoPlan(
        preset=preset,
        width=width,
        height=height,
        frame_rate_cap=MAX_FRAME_RATE if analysis.frame_rate > MAX_FRAME_RATE + 0.5 else None,
        video_bitrate=target,
        maxrate=int(target * 1.5),
        bufsize=int(target * 2),
        audio=audio,
        estimated_bytes=estimated,
        worthwhile=worthwhile,
        threads=encode_threads(analysis.width, analysis.height, cpus),
    )


def plan_all(analysis: VideoAnalysis) -> dict[Preset, VideoPlan]:
    return {preset: plan_video(analysis, preset) for preset in Preset}


def build_video_args(
    ffmpeg_bin: str,
    source: Path,
    destination: Path,
    analysis: VideoAnalysis,
    plan: VideoPlan,
) -> list[str]:
    """H.264 + AAC in a faststart MP4, upright, at the planned size and bitrate.

    FFmpeg applies the display matrix while decoding, so scaling works on
    upright frames and the output needs no rotation tag. Container metadata,
    chapters, subtitles, data streams and cover art are all dropped, and the
    muxer is told not to stamp its own version in - nothing is invented.
    """
    filters = [f"scale={plan.width}:{plan.height}:flags=lanczos", "setsar=1"]
    if plan.frame_rate_cap:
        filters.append(f"fps={plan.frame_rate_cap:g}")
    filters.append("format=yuv420p")

    threads = str(plan.threads)
    args = [
        ffmpeg_bin, "-y", "-hide_banner", "-nostdin",
        "-loglevel", "error", "-nostats", "-progress", "pipe:1",
        # Before -i: the decoder's threads (and so its frame buffers).
        "-threads", threads,
        "-i", str(source),
        "-map", f"0:{analysis.video_stream}",
    ]
    if plan.audio is not None and analysis.audio_stream is not None:
        args += ["-map", f"0:{analysis.audio_stream}"]
    args += [
        "-sn", "-dn",
        "-map_metadata", "-1",
        "-map_chapters", "-1",
        "-filter_threads", threads,
        "-vf", ",".join(filters),
        "-c:v", "libx264",
        "-preset", PROFILES[plan.preset].x264_preset,
        # After -i: the encoders' threads.
        "-threads", threads,
        "-rc-lookahead", str(RC_LOOKAHEAD),
        "-profile:v", "high",
        "-b:v", str(plan.video_bitrate),
        "-maxrate", str(plan.maxrate),
        "-bufsize", str(plan.bufsize),
        "-metadata:s:v", "rotate=0",
    ]
    if plan.audio is None:
        args += ["-an"]
    elif plan.audio.copy:
        args += ["-c:a", "copy"]
    else:
        args += ["-c:a", "aac", "-b:a", str(plan.audio.bitrate)]
        if plan.audio.downmix:
            args += ["-ac", "2"]
    args += [
        "-movflags", "+faststart",
        "-fflags", "+bitexact",
        "-f", "mp4",
        str(destination),
    ]
    return args


def build_deep_png_args(ffmpeg_bin: str, source: Path, destination: Path) -> list[str]:
    """Lossless PNG re-compression that keeps all 16 bits per channel."""
    return [
        ffmpeg_bin, "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
        "-i", str(source),
        "-map", "0:v:0", "-frames:v", "1",
        "-map_metadata", "-1",
        "-c:v", "png", "-pred", "mixed", "-compression_level", "9",
        "-f", "image2", "-update", "1",
        str(destination),
    ]


def build_image_worker_args(python_bin: str, source: Path, destination: Path,
                            image_format: str, preset: Preset) -> list[str]:
    return [python_bin, str(IMAGE_WORKER), str(source), str(destination), image_format, preset.value]


# --- output naming and verification (pure) -----------------------------------


def optimized_filename(
    original_filename: str | None,
    kind: MediaKind,
    image_format: str | None = None,
    *,
    prefix: str = OUTPUT_PREFIX,
) -> str:
    """``<prefix><original name>``, sanitised, with the real extension."""
    if kind is MediaKind.VIDEO:
        extension, fallback = ".mp4", "video"
    else:
        extension, fallback = IMAGE_EXTENSIONS.get(image_format or "jpeg", ".jpg"), "photo"
        original_ext = Path(original_filename or "").suffix.lower()
        if image_format == "jpeg" and original_ext in (".jpg", ".jpeg"):
            extension = original_ext
    basename = (original_filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    stem = basename.rsplit(".", 1)[0] if "." in basename else basename
    stem = safe_display_name(stem, fallback)
    if not any(char.isalnum() for char in stem):
        # e.g. a name written entirely in a non-Latin script.
        stem = fallback
    return f"{prefix}{stem}{extension}"


def verify_video(output: dict[str, Any], analysis: VideoAnalysis, plan: VideoPlan) -> None:
    """The file must open and be exactly what was planned."""
    fmt = output.get("format") or {}
    streams = output.get("streams") or []
    videos = [s for s in streams if s.get("codec_type") == "video"]
    audios = [s for s in streams if s.get("codec_type") == "audio"]

    def fail(detail: str) -> None:
        raise MediaProcessingError(ProcessingErrorCode.VERIFY_FAILED, detail)

    if "mp4" not in _names(str(fmt.get("format_name") or "")):
        fail(f"container {fmt.get('format_name')!r}")
    if len(videos) != 1 or videos[0].get("codec_name") != "h264":
        fail("expected exactly one h264 stream")
    video = videos[0]
    if (int(video.get("width") or 0), int(video.get("height") or 0)) != (plan.width, plan.height):
        fail(f"dimensions {video.get('width')}x{video.get('height')}, planned {plan.width}x{plan.height}")
    if _stream_rotation(video):
        fail("output still carries a rotation")
    if plan.audio is None and audios:
        fail("a silent source gained an audio track")
    if plan.audio is not None and [a.get("codec_name") for a in audios] != ["aac"]:
        fail("audio was not kept as one AAC track")
    duration = _number(fmt.get("duration"))
    if analysis.duration and abs(duration - analysis.duration) > max(1.0, analysis.duration * 0.03):
        fail(f"duration {duration:.2f}s, source {analysis.duration:.2f}s")


def verify_image(output: dict[str, Any], analysis: ImageAnalysis) -> None:
    streams = [s for s in output.get("streams") or [] if s.get("codec_type") == "video"]

    def fail(detail: str) -> None:
        raise MediaProcessingError(ProcessingErrorCode.VERIFY_FAILED, detail)

    if len(streams) != 1:
        fail("expected one picture")
    stream = streams[0]
    if IMAGE_CODECS.get(str(stream.get("codec_name"))) != analysis.format:
        fail(f"format changed to {stream.get('codec_name')!r}")
    if (int(stream.get("width") or 0), int(stream.get("height") or 0)) != (analysis.width, analysis.height):
        fail("pixel dimensions changed")
    if analysis.has_alpha and analysis.pix_fmt != "pal8" and str(stream.get("pix_fmt")) not in ALPHA_PIX_FMTS:
        fail("transparency was lost")


_KILLED_RETURN_CODES = (-9, 137)  # SIGKILL, as asyncio and as a shell report it


def classify_failure(stderr: str, returncode: int | None = None) -> ProcessingErrorCode:
    if returncode in _KILLED_RETURN_CODES:
        # Nothing on stderr: the process never got to say anything.
        return ProcessingErrorCode.PROCESS_KILLED
    text = (stderr or "").lower()
    if any(marker in text for marker in _NO_SPACE_MARKERS):
        return ProcessingErrorCode.DISK_FULL
    return classify_encode_failure(stderr)


# --- the service ------------------------------------------------------------


@dataclass(frozen=True)
class OptimizedMedia:
    path: Path
    size_bytes: int
    kind: MediaKind
    width: int
    height: int
    duration: float = 0.0


ProgressCallback = Callable[[float], Awaitable[None]]

_WORKER_EXIT_CODES = {
    2: ProcessingErrorCode.UNSUPPORTED,
    3: ProcessingErrorCode.TOO_LARGE,
    4: ProcessingErrorCode.CORRUPT_INPUT,
}


class MediaOptimizerService:
    def __init__(
        self,
        *,
        ffmpeg_bin: str,
        ffprobe_bin: str,
        timeout: float,
        probe_timeout: float,
        python_bin: str = sys.executable,
    ) -> None:
        self._ffmpeg_bin = ffmpeg_bin
        self._ffprobe_bin = ffprobe_bin
        self._timeout = timeout
        self._probe_timeout = probe_timeout
        self._python_bin = python_bin

    async def analyze(self, source: Path, *, job_id: str | None = None) -> Analysis:
        analysis = parse_analysis(await self._probe(source, job_id=job_id), source.stat().st_size)
        logger.info("optimizer input %s", _describe(analysis), extra={"job_id": job_id or "-"})
        return analysis

    async def optimize(
        self,
        source: Path,
        workspace: JobWorkspace,
        analysis: Analysis,
        preset: Preset,
        *,
        job_id: str | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> OptimizedMedia:
        if isinstance(analysis, VideoAnalysis):
            return await self._optimize_video(source, workspace, analysis, preset, job_id, on_progress)
        return await self._optimize_image(source, workspace, analysis, preset, job_id)

    async def _optimize_video(self, source, workspace, analysis, preset, job_id, on_progress):
        plan = plan_video(analysis, preset)
        destination = workspace.new_file(".mp4", prefix="opt_")
        args = build_video_args(self._ffmpeg_bin, source, destination, analysis, plan)
        logger.info(
            "optimizer encode preset=%s %dx%d -> %dx%d threads=%d lookahead=%d",
            preset.value, analysis.width, analysis.height, plan.width, plan.height,
            plan.threads, RC_LOOKAHEAD, extra={"job_id": job_id or "-"},
        )
        await self._run_ffmpeg(args, analysis.duration, job_id, on_progress)
        self._ensure_written(destination)

        verify_video(json.loads(await self._probe(destination, job_id=job_id)), analysis, plan)
        size = destination.stat().st_size
        logger.info(
            "video optimized preset=%s %dx%d %dk -> %d bytes (from %d)",
            preset.value, plan.width, plan.height, plan.video_bitrate // 1000, size,
            analysis.size_bytes, extra={"job_id": job_id or "-"},
        )
        return OptimizedMedia(destination, size, MediaKind.VIDEO, plan.width, plan.height,
                              analysis.duration)

    async def _optimize_image(self, source, workspace, analysis, preset, job_id):
        destination = workspace.new_file(IMAGE_EXTENSIONS[analysis.format], prefix="opt_")
        if analysis.deep_png:
            args = build_deep_png_args(self._ffmpeg_bin, source, destination)
        else:
            args = build_image_worker_args(self._python_bin, source, destination,
                                           analysis.format, preset)
        try:
            await run_command(args, timeout=self._timeout, job_id=job_id)
        except CommandNotFound as exc:
            raise MediaProcessingError(ProcessingErrorCode.TOOL_MISSING, str(exc)) from exc
        except CommandTimeout as exc:
            raise MediaProcessingError(ProcessingErrorCode.TIMEOUT, str(exc)) from exc
        except CommandFailed as exc:
            code = _WORKER_EXIT_CODES.get(exc.returncode) if not analysis.deep_png else None
            code = code or classify_failure(exc.stderr, exc.returncode)
            raise MediaProcessingError(code, str(exc)) from exc
        self._ensure_written(destination)

        verify_image(json.loads(await self._probe(destination, job_id=job_id)), analysis)
        size = destination.stat().st_size
        logger.info(
            "image optimized preset=%s %s %dx%d -> %d bytes (from %d)",
            preset.value, analysis.format, analysis.width, analysis.height, size,
            analysis.size_bytes, extra={"job_id": job_id or "-"},
        )
        return OptimizedMedia(destination, size, MediaKind.IMAGE, analysis.width, analysis.height)

    async def _run_ffmpeg(self, args, duration, job_id, on_progress) -> None:
        async def on_line(line: str) -> None:
            # "out_time_us" is FFmpeg's position in the output, in microseconds.
            if on_progress is None or not duration or not line.startswith("out_time_us="):
                return
            position = _number(line.partition("=")[2]) / 1_000_000
            await on_progress(min(1.0, position / duration))

        try:
            await run_command_streaming(args, timeout=self._timeout, on_line=on_line, job_id=job_id)
        except CommandNotFound as exc:
            raise MediaProcessingError(ProcessingErrorCode.TOOL_MISSING, str(exc)) from exc
        except CommandTimeout as exc:
            raise MediaProcessingError(ProcessingErrorCode.TIMEOUT, str(exc)) from exc
        except CommandFailed as exc:
            raise MediaProcessingError(classify_failure(exc.stderr, exc.returncode), str(exc)) from exc

    @staticmethod
    def _ensure_written(destination: Path) -> None:
        if not destination.exists() or destination.stat().st_size == 0:
            raise MediaProcessingError(ProcessingErrorCode.EMPTY_OUTPUT, "empty optimizer output")

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


def _describe(analysis: Analysis) -> str:
    if isinstance(analysis, VideoAnalysis):
        return (
            f"video {analysis.width}x{analysis.height} {analysis.duration:.1f}s "
            f"{analysis.video_codec} {analysis.frame_rate:.2f}fps {analysis.video_bitrate // 1000}k "
            f"audio={analysis.audio_codec or '-'} rot={analysis.rotation} {analysis.size_bytes}B"
        )
    return (
        f"image {analysis.format} {analysis.width}x{analysis.height} {analysis.pix_fmt} "
        f"{analysis.size_bytes}B"
    )
