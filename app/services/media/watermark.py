"""Watermarking: a creator's handle on their own photos and videos.

The watermark is deliberately modest - one short line, sized as a share of the
frame height (so it looks the same on a 720p clip and a 4K one), inset from the
edge, optionally with a soft shadow so it stays readable over both light and
dark footage. Nothing is scaled, cropped or re-timed.

Photos go through a Pillow worker in its own process (``watermark_worker.py``);
videos through FFmpeg's ``drawtext``, under the same explicit thread and
lookahead budget the optimizer uses, so a 4K video cannot exhaust the
container's memory.

Both paths draw with the *same* TrueType font, which is what lets the layout be
planned once, here: the text is measured with Pillow and the font shrunk until
it fits the frame, and FFmpeg is then handed that exact pixel size.

FFmpeg filter arguments are notoriously hard to quote - a Windows path alone
carries both a colon and backslashes - so the text and the font are placed in
the job workspace and FFmpeg runs *inside* it, referring to them by plain
relative names.
"""

from __future__ import annotations

import enum
import json
import logging
import os
import shutil
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.services.media.base import MediaProcessingError, ProcessedFile, ProcessingErrorCode
from app.services.media.optimizer import (
    ALPHA_PIX_FMTS,
    Analysis,
    IMAGE_CODECS,
    IMAGE_EXTENSIONS,
    ImageAnalysis,
    MediaKind,
    RC_LOOKAHEAD,
    VideoAnalysis,
    classify_failure,
    encode_threads,
    optimized_filename,
    parse_analysis,
)
from app.services.media.probe import build_ffprobe_args
from app.utils.subprocess import (
    CommandFailed,
    CommandNotFound,
    CommandTimeout,
    run_command,
    run_command_streaming,
)
from app.utils.temp_files import JobWorkspace, safe_display_name

logger = logging.getLogger(__name__)

WORKER = Path(__file__).with_name("watermark_worker.py")

OUTPUT_PREFIX = "atreox_watermarked_"

# One short line of branding. Long enough for "t.me/averylongchannelname",
# short enough that it never becomes a banner.
MAX_TEXT_LENGTH = 48

# Inside the workspace, so FFmpeg never has to quote a path.
TEXT_FILENAME = "watermark.txt"
FONT_FILENAME = "watermark_font.ttf"

# How watermark_worker.py reports why it could not draw.
WORKER_EXIT_CODES = {
    2: ProcessingErrorCode.UNSUPPORTED,
    3: ProcessingErrorCode.TOO_LARGE,
    4: ProcessingErrorCode.CORRUPT_INPUT,
}


class Position(str, enum.Enum):
    TOP_LEFT = "top_left"
    TOP_RIGHT = "top_right"
    BOTTOM_LEFT = "bottom_left"
    BOTTOM_RIGHT = "bottom_right"
    BOTTOM_CENTER = "bottom_center"


class Style(str, enum.Enum):
    WHITE = "white"
    BLACK = "black"
    WHITE_SHADOW = "white_shadow"


class Size(str, enum.Enum):
    S = "s"
    M = "m"
    L = "l"


# Share of the frame height, so the watermark reads the same at any resolution.
SIZE_RATIOS: dict[Size, float] = {Size.S: 0.025, Size.M: 0.035, Size.L: 0.05}
OPACITIES = (25, 50, 75, 100)

DEFAULT_POSITION = Position.BOTTOM_RIGHT
DEFAULT_STYLE = Style.WHITE_SHADOW
DEFAULT_SIZE = Size.M
DEFAULT_OPACITY = 75

# Small frames still need legible text.
MIN_FONT_SIZE = 11
# Inset from the edges, as a share of the shorter side.
_PADDING_RATIO = 0.02
_MIN_PADDING = 6
# The shadow sits just under the text.
_SHADOW_RATIO = 0.08
# A shadow is there to lift the text off the picture, not to be seen itself.
_SHADOW_OPACITY = 0.55

# Fonts that ship with the platforms this runs on. The Docker image installs
# fonts-dejavu-core, which provides the first two.
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
)

# Invisible characters are classified rather than listed: a "Cf" character
# (zero-width joiner, bidi override) would let a watermark hide or
# misrepresent itself and is dropped, while control and line separators
# become spaces - so a pasted second line joins the first as a line, not as
# one run-on word.
_DROPPED_CATEGORIES = frozenset({"Cf"})
_SPACED_CATEGORIES = frozenset({"Cc", "Zl", "Zp"})


def clean_watermark_text(raw: str | None) -> str | None:
    """A single tidy line, or ``None`` when it is not usable as a watermark."""
    if not raw:
        return None
    characters = []
    for character in unicodedata.normalize("NFC", raw):
        category = unicodedata.category(character)
        if category in _DROPPED_CATEGORIES:
            continue
        characters.append(" " if category in _SPACED_CATEGORIES else character)
    text = " ".join("".join(characters).split())  # one line, single spaces
    if not text or len(text) > MAX_TEXT_LENGTH:
        return None
    if not any(character.isalnum() for character in text):
        return None
    return text


@dataclass(frozen=True)
class WatermarkSpec:
    """What to draw, and how it should look."""

    text: str
    position: Position = DEFAULT_POSITION
    style: Style = DEFAULT_STYLE
    size: Size = DEFAULT_SIZE
    opacity: int = DEFAULT_OPACITY

    @property
    def alpha(self) -> float:
        return max(0.05, min(100, self.opacity)) / 100

    @property
    def shadowed(self) -> bool:
        return self.style is Style.WHITE_SHADOW

    @property
    def colour(self) -> str:
        return "black" if self.style is Style.BLACK else "white"

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "position": self.position.value,
            "style": self.style.value,
            "size": self.size.value,
            "opacity": self.opacity,
        }

    @classmethod
    def from_dict(cls, payload: dict | None) -> WatermarkSpec | None:
        if not payload or not payload.get("text"):
            return None
        try:
            return cls(
                text=str(payload["text"]),
                position=Position(payload.get("position", DEFAULT_POSITION.value)),
                style=Style(payload.get("style", DEFAULT_STYLE.value)),
                size=Size(payload.get("size", DEFAULT_SIZE.value)),
                opacity=int(payload.get("opacity", DEFAULT_OPACITY)),
            )
        except (KeyError, TypeError, ValueError):
            return None


def resolve_font(
    configured: str | None = None,
    *,
    candidates: tuple[str, ...] = FONT_CANDIDATES,
    exists: Callable[[str], bool] = os.path.isfile,
) -> str:
    """The TrueType font both paths draw with."""
    for candidate in ([configured] if configured else []) + list(candidates):
        if candidate and exists(candidate):
            return candidate
    raise MediaProcessingError(
        ProcessingErrorCode.TOOL_MISSING, "no watermark font found on this system"
    )


@dataclass(frozen=True)
class Layout:
    """Where the text goes, in pixels, for one particular frame size."""

    font_size: int
    padding: int
    shadow_offset: int


def padding_for(width: int, height: int) -> int:
    return max(_MIN_PADDING, round(min(width, height) * _PADDING_RATIO))


def fit_font_size(
    *,
    width: int,
    height: int,
    ratio: float,
    padding: int,
    measure: Callable[[int], float],
) -> int:
    """The nominal size for this frame, shrunk until the line fits across it.

    ``measure(size)`` returns how wide the text renders at ``size``; a long
    handle on a narrow portrait frame therefore gets smaller text rather than
    text running off the picture.
    """
    available = max(1, width - 2 * padding)
    size = max(MIN_FONT_SIZE, round(height * ratio))
    while size > MIN_FONT_SIZE and measure(size) > available:
        # Proportional guess, then a steady walk down for the last few pixels.
        size = min(size - 1, max(MIN_FONT_SIZE, int(size * available / measure(size))))
    return size


def text_measurer(font_path: str, text: str) -> Callable[[int], float]:
    """Measures ``text`` with the real font - the same one FFmpeg will use."""
    from PIL import ImageFont

    def measure(size: int) -> float:
        return ImageFont.truetype(font_path, size).getlength(text)

    return measure


def plan_layout(width: int, height: int, spec: WatermarkSpec, font_path: str) -> Layout:
    padding = padding_for(width, height)
    font_size = fit_font_size(
        width=width,
        height=height,
        ratio=SIZE_RATIOS[spec.size],
        padding=padding,
        measure=text_measurer(font_path, spec.text),
    )
    return Layout(
        font_size=font_size,
        padding=padding,
        shadow_offset=max(1, round(font_size * _SHADOW_RATIO)),
    )


# --- video ------------------------------------------------------------------

# drawtext expressions per position; "P" is the padding, filled in below.
_DRAWTEXT_XY: dict[Position, tuple[str, str]] = {
    Position.TOP_LEFT: ("{p}", "{p}"),
    Position.TOP_RIGHT: ("w-text_w-{p}", "{p}"),
    Position.BOTTOM_LEFT: ("{p}", "h-text_h-{p}"),
    Position.BOTTOM_RIGHT: ("w-text_w-{p}", "h-text_h-{p}"),
    Position.BOTTOM_CENTER: ("(w-text_w)/2", "h-text_h-{p}"),
}


def build_drawtext(spec: WatermarkSpec, layout: Layout) -> str:
    """One ``drawtext`` filter, reading its text and font from the workspace."""
    x, y = (part.format(p=layout.padding) for part in _DRAWTEXT_XY[spec.position])
    options = [
        f"textfile={TEXT_FILENAME}",
        f"fontfile={FONT_FILENAME}",
        f"fontsize={layout.font_size}",
        f"fontcolor={spec.colour}@{spec.alpha:.2f}",
        f"x={x}",
        f"y={y}",
    ]
    if spec.shadowed:
        options += [
            f"shadowcolor=black@{spec.alpha * _SHADOW_OPACITY:.2f}",
            f"shadowx={layout.shadow_offset}",
            f"shadowy={layout.shadow_offset}",
        ]
    return "drawtext=" + ":".join(options)


@dataclass(frozen=True)
class VideoPlan:
    width: int
    height: int
    layout: Layout
    threads: int
    copy_audio: bool
    has_audio: bool
    maxrate: int | None
    x264_preset: str
    lookahead: int


def _even(value: int) -> int:
    return max(2, value // 2 * 2)


# Audio that can stay exactly as it is inside an MP4.
_COPYABLE_AUDIO = frozenset({"aac", "mp3"})

# Watermarking keeps the original resolution, so a 4K frame is encoded as 4K -
# and x264's frame buffers scale with it. Measured peak RSS on a 2160x3840
# 60 fps source, in the bot's 1 GB container:
#   faster + lookahead 10    964 MB   (over budget: the kernel would step in)
#   ultrafast, no lookahead  352 MB   (and six times faster)
# So frames above 1080p use the light encoder, with the bitrate cap below
# keeping the result close to the source; everything smaller keeps the
# quality-oriented settings (1080p measured 267 MB).
LARGE_FRAME_PIXELS = 1920 * 1088
_LARGE_FRAME_ENCODER = ("ultrafast", 0)
_DEFAULT_ENCODER = ("faster", RC_LOOKAHEAD)
# Quality-targeted encode: the picture is barely changed, so this stays visually
# transparent, while the bitrate cap keeps the file from ballooning.
VIDEO_CRF = 21


def plan_video(analysis: VideoAnalysis, spec: WatermarkSpec, font_path: str) -> VideoPlan:
    """Same resolution, same frame rate, same duration - only pixels change."""
    width, height = _even(analysis.width), _even(analysis.height)
    preset, lookahead = (
        _LARGE_FRAME_ENCODER if width * height > LARGE_FRAME_PIXELS else _DEFAULT_ENCODER
    )
    return VideoPlan(
        width=width,
        height=height,
        layout=plan_layout(width, height, spec, font_path),
        threads=encode_threads(width, height),
        copy_audio=analysis.audio_codec in _COPYABLE_AUDIO,
        has_audio=analysis.has_audio,
        # The picture is unchanged apart from one line of text, so whatever the
        # source spent is the right budget - and keeps the file from growing.
        maxrate=analysis.video_bitrate or None,
        x264_preset=preset,
        lookahead=lookahead,
    )


def build_video_args(
    ffmpeg_bin: str,
    source: Path,
    destination: Path,
    analysis: VideoAnalysis,
    plan: VideoPlan,
    spec: WatermarkSpec,
) -> list[str]:
    """Draw the watermark and re-encode the video, changing nothing else.

    Run with the workspace as the working directory (see the module docstring).
    FFmpeg applies the display matrix while decoding, so the text lands the
    right way up and the output needs no rotation tag.
    """
    threads = str(plan.threads)
    filters = [
        f"scale={plan.width}:{plan.height}",
        "setsar=1",
        build_drawtext(spec, plan.layout),
        "format=yuv420p",
    ]
    args = [
        ffmpeg_bin, "-y", "-hide_banner", "-nostdin",
        "-loglevel", "error", "-nostats", "-progress", "pipe:1",
        "-threads", threads,
        "-i", str(source),
        "-map", f"0:{analysis.video_stream}",
    ]
    if plan.has_audio and analysis.audio_stream is not None:
        args += ["-map", f"0:{analysis.audio_stream}"]
    args += [
        "-sn", "-dn",
        "-map_metadata", "-1",
        "-map_chapters", "-1",
        "-filter_threads", threads,
        "-vf", ",".join(filters),
        "-c:v", "libx264",
        "-preset", plan.x264_preset,
        "-threads", threads,
        "-rc-lookahead", str(plan.lookahead),
        "-profile:v", "high",
        "-crf", str(VIDEO_CRF),
        "-metadata:s:v", "rotate=0",
    ]
    if plan.maxrate:
        args += ["-maxrate", str(plan.maxrate), "-bufsize", str(plan.maxrate * 2)]
    if not plan.has_audio:
        args += ["-an"]
    elif plan.copy_audio:
        # The soundtrack is untouched, so it is copied through verbatim.
        args += ["-c:a", "copy"]
    else:
        args += ["-c:a", "aac", "-b:a", "160k"]
    args += ["-movflags", "+faststart", "-fflags", "+bitexact", "-f", "mp4", str(destination)]
    return args


def verify_video(output: dict[str, Any], analysis: VideoAnalysis, plan: VideoPlan) -> None:
    fmt = output.get("format") or {}
    streams = output.get("streams") or []
    videos = [s for s in streams if s.get("codec_type") == "video"]
    audios = [s for s in streams if s.get("codec_type") == "audio"]

    def fail(detail: str) -> None:
        raise MediaProcessingError(ProcessingErrorCode.VERIFY_FAILED, detail)

    if len(videos) != 1 or videos[0].get("codec_name") != "h264":
        fail("expected exactly one h264 stream")
    video = videos[0]
    if (int(video.get("width") or 0), int(video.get("height") or 0)) != (plan.width, plan.height):
        fail(f"resolution changed to {video.get('width')}x{video.get('height')}")
    if plan.has_audio and not audios:
        fail("the audio track was lost")
    if not plan.has_audio and audios:
        fail("a silent video gained audio")
    duration = float(fmt.get("duration") or 0)
    if analysis.duration and abs(duration - analysis.duration) > max(1.0, analysis.duration * 0.03):
        fail(f"duration {duration:.2f}s, source {analysis.duration:.2f}s")


def verify_image(output: dict[str, Any], analysis: ImageAnalysis) -> None:
    """Same format, same number of pixels, transparency still there.

    The picture may be *stored* rotated (a phone photo with an EXIF
    orientation): drawing upright text means writing those pixels upright, so
    width and height can swap. What must not change is how many pixels there
    are - nothing is resized, cropped or upscaled.
    """
    streams = [s for s in output.get("streams") or [] if s.get("codec_type") == "video"]

    def fail(detail: str) -> None:
        raise MediaProcessingError(ProcessingErrorCode.VERIFY_FAILED, detail)

    if len(streams) != 1:
        fail("expected one picture")
    stream = streams[0]
    if IMAGE_CODECS.get(str(stream.get("codec_name"))) != analysis.format:
        fail(f"format changed to {stream.get('codec_name')!r}")
    width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
    if sorted((width, height)) != sorted((analysis.width, analysis.height)):
        fail(f"dimensions {width}x{height}, source {analysis.width}x{analysis.height}")
    if analysis.has_alpha and analysis.pix_fmt != "pal8" and str(stream.get("pix_fmt")) not in ALPHA_PIX_FMTS:
        fail("transparency was lost")


def watermarked_filename(original_filename: str | None, kind: MediaKind,
                         image_format: str | None = None) -> str:
    """``atreox_watermarked_<original name>``, sanitised, with a real extension."""
    return optimized_filename(original_filename, kind, image_format, prefix=OUTPUT_PREFIX)


# --- the service ------------------------------------------------------------


@dataclass(frozen=True)
class WatermarkedMedia(ProcessedFile):
    kind: MediaKind = MediaKind.IMAGE
    width: int = 0
    height: int = 0


class WatermarkService:
    def __init__(
        self,
        *,
        ffmpeg_bin: str,
        ffprobe_bin: str,
        timeout: float,
        probe_timeout: float,
        font: str | None = None,
        python_bin: str = sys.executable,
    ) -> None:
        self._ffmpeg_bin = ffmpeg_bin
        self._ffprobe_bin = ffprobe_bin
        self._timeout = timeout
        self._probe_timeout = probe_timeout
        self._font = font
        self._python_bin = python_bin

    @property
    def font_path(self) -> str:
        return resolve_font(self._font)

    async def analyze(self, source: Path, *, job_id: str | None = None) -> Analysis:
        return parse_analysis(await self._probe(source, job_id=job_id), source.stat().st_size)

    async def apply(
        self,
        source: Path,
        workspace: JobWorkspace,
        analysis: Analysis,
        spec: WatermarkSpec,
        *,
        job_id: str | None = None,
        on_progress: Any | None = None,
    ) -> WatermarkedMedia:
        if isinstance(analysis, VideoAnalysis):
            return await self._apply_to_video(source, workspace, analysis, spec, job_id, on_progress)
        return await self._apply_to_image(source, workspace, analysis, spec, job_id)

    async def _apply_to_video(self, source, workspace, analysis, spec, job_id, on_progress):
        plan = plan_video(analysis, spec, self.font_path)
        # Text and font live in the workspace; FFmpeg runs there and refers to
        # them by name, so no path ever reaches the filter graph.
        (workspace.path / TEXT_FILENAME).write_text(spec.text, encoding="utf-8")
        shutil.copyfile(self.font_path, workspace.path / FONT_FILENAME)
        destination = workspace.new_file(".mp4", prefix="wm_")
        args = build_video_args(self._ffmpeg_bin, source, destination, analysis, plan, spec)
        logger.info(
            "watermark encode %dx%d font=%dpx threads=%d audio=%s",
            plan.width, plan.height, plan.layout.font_size, plan.threads,
            "copy" if plan.copy_audio else ("aac" if plan.has_audio else "none"),
            extra={"job_id": job_id or "-"},
        )

        async def on_line(line: str) -> None:
            if on_progress is None or not analysis.duration or not line.startswith("out_time_us="):
                return
            try:
                position = max(0.0, float(line.partition("=")[2])) / 1_000_000
            except ValueError:
                return
            await on_progress(min(1.0, position / analysis.duration))

        try:
            await run_command_streaming(
                args, timeout=self._timeout, on_line=on_line, job_id=job_id, cwd=workspace.path
            )
        except CommandNotFound as exc:
            raise MediaProcessingError(ProcessingErrorCode.TOOL_MISSING, str(exc)) from exc
        except CommandTimeout as exc:
            raise MediaProcessingError(ProcessingErrorCode.TIMEOUT, str(exc)) from exc
        except CommandFailed as exc:
            raise MediaProcessingError(classify_failure(exc.stderr, exc.returncode), str(exc)) from exc

        self._ensure_written(destination)
        verify_video(json.loads(await self._probe(destination, job_id=job_id)), analysis, plan)
        size = destination.stat().st_size
        logger.info("watermarked video ready (%d bytes)", size, extra={"job_id": job_id or "-"})
        return WatermarkedMedia(
            path=destination, size_bytes=size, filename=destination.name,
            kind=MediaKind.VIDEO, width=plan.width, height=plan.height,
        )

    async def _apply_to_image(self, source, workspace, analysis, spec, job_id):
        layout = plan_layout(analysis.width, analysis.height, spec, self.font_path)
        destination = workspace.new_file(IMAGE_EXTENSIONS[analysis.format], prefix="wm_")
        request = workspace.path / "watermark.json"
        request.write_text(
            json.dumps({
                "source": str(source),
                "destination": str(destination),
                "format": analysis.format,
                "font": self.font_path,
                "text": spec.text,
                "position": spec.position.value,
                "colour": spec.colour,
                "alpha": spec.alpha,
                "shadow": spec.shadowed,
                "shadow_alpha": spec.alpha * _SHADOW_OPACITY,
                "font_size": layout.font_size,
                "padding": layout.padding,
                "shadow_offset": layout.shadow_offset,
            }),
            encoding="utf-8",
        )
        try:
            result = await run_command(
                [self._python_bin, str(WORKER), str(request)],
                timeout=self._timeout,
                job_id=job_id,
            )
        except CommandNotFound as exc:
            raise MediaProcessingError(ProcessingErrorCode.TOOL_MISSING, str(exc)) from exc
        except CommandTimeout as exc:
            raise MediaProcessingError(ProcessingErrorCode.TIMEOUT, str(exc)) from exc
        except CommandFailed as exc:
            code = WORKER_EXIT_CODES.get(exc.returncode) or classify_failure(
                exc.stderr, exc.returncode
            )
            raise MediaProcessingError(code, str(exc)) from exc

        self._ensure_written(destination)
        rendered = json.loads(result.stdout or "{}")
        verify_image(json.loads(await self._probe(destination, job_id=job_id)), analysis)
        size = destination.stat().st_size
        logger.info(
            "watermarked %s ready (%dx%d, %d bytes)", analysis.format,
            rendered.get("width"), rendered.get("height"), size,
            extra={"job_id": job_id or "-"},
        )
        return WatermarkedMedia(
            path=destination, size_bytes=size, filename=destination.name,
            kind=MediaKind.IMAGE, width=int(rendered["width"]), height=int(rendered["height"]),
        )

    @staticmethod
    def _ensure_written(destination: Path) -> None:
        if not destination.exists() or destination.stat().st_size == 0:
            raise MediaProcessingError(ProcessingErrorCode.EMPTY_OUTPUT, "empty watermark output")

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
