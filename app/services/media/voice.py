"""Audio / video -> Telegram voice message.

Telegram draws the voice bubble (waveform, speed control) only for an OGG file
carrying Opus, so every input - an MP3, a FLAC, the sound track of a MOV - is
re-encoded to exactly that: mono, 48 kHz Opus tuned for speech.

The signal is only transcoded. Nothing changes its pitch, tempo or character,
and there is no loudness normalisation.

As everywhere else, the FFmpeg invocation and the ffprobe parsing are pure
functions; the service is a thin async runner around them. The encoded file
is probed again before it is handed back, so a voice note that is not really
Opus - or is empty - never reaches ``sendVoice``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from app.services.media.base import MediaProcessingError, ProcessedFile, ProcessingErrorCode
from app.services.media.probe import build_ffprobe_args
from app.utils.subprocess import (
    CommandFailed,
    CommandNotFound,
    CommandTimeout,
    run_command,
)

logger = logging.getLogger(__name__)

# Opus always runs at 48 kHz internally; resampling up front keeps libopus from
# picking a narrower band for low-rate sources.
VOICE_SAMPLE_RATE = 48000
VOICE_CHANNELS = 1
# Transparent for speech in mono Opus and still good for music, at roughly
# 21 MB per hour - comfortably inside any upload limit.
VOICE_BITRATE = "48k"

# Anything shorter than this after encoding is treated as "no audio at all".
_MIN_VOICE_SECONDS = 0.05

# What FFmpeg prints when the input, not the encoder, is at fault. A file can
# pass ffprobe (which only reads headers) and still fail to decode.
_DECODE_FAILURE_MARKERS = (
    "invalid data found when processing input",
    "decode error rate",
    "error while decoding",
    "error submitting packet to decoder",
    "header missing",
)

# Demuxers FFmpeg may use on user input, as ffprobe names them. Anything else is
# refused before encoding: playlists (hls), text formats and image sequences
# have no business here, and a playlist can make FFmpeg open other files.
ALLOWED_INPUT_FORMATS = frozenset(
    {
        # audio
        "mp3", "wav", "w64", "flac", "ogg", "aac", "aiff", "caf", "amr",
        "amrnb", "amrwb", "wv", "ape", "ac3", "eac3", "dts", "au",
        # containers that carry audio, video or both
        "mov", "mp4", "m4a", "3gp", "3g2", "mj2",
        "matroska", "webm", "avi", "asf", "flv", "mpegts", "mpeg",
    }
)


@dataclass(frozen=True)
class AudioSourceInfo:
    """What ffprobe told us about the audio a voice note will be made from."""

    duration: float
    audio_codec: str
    channels: int | None = None
    sample_rate: int | None = None
    format_name: str = ""
    has_video: bool = False


@dataclass(frozen=True)
class VoiceOutputInfo:
    """What ffprobe reports about an encoded voice note."""

    format_name: str
    codec: str | None
    channels: int | None
    sample_rate: int | None
    duration: float


@dataclass(frozen=True)
class VoiceNoteResult(ProcessedFile):
    """An encoded voice note, with the duration ``sendVoice`` is told about."""

    duration: float = 0.0


def _load(payload: str) -> dict[str, Any]:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise MediaProcessingError(ProcessingErrorCode.PROBE_FAILED, "invalid ffprobe json") from exc
    return data if isinstance(data, dict) else {}


def _as_float(value: Any) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_picture(stream: dict[str, Any]) -> bool:
    """Cover art in an MP3/M4A shows up as a one-frame video stream."""
    return bool((stream.get("disposition") or {}).get("attached_pic"))


def format_is_allowed(format_name: str | None) -> bool:
    """ffprobe lists every name a demuxer answers to, e.g. ``mov,mp4,m4a``."""
    names = {name.strip() for name in (format_name or "").split(",") if name.strip()}
    return bool(names & ALLOWED_INPUT_FORMATS)


def parse_audio_source(payload: str) -> AudioSourceInfo:
    """Turn ffprobe JSON into :class:`AudioSourceInfo`, or say why we can't.

    * a demuxer outside the allowlist -> ``UNSUPPORTED``
    * real video but no audio stream  -> ``NO_AUDIO``
    * neither                          -> ``UNSUPPORTED``
    """
    data = _load(payload)
    fmt = data.get("format") or {}
    format_name = str(fmt.get("format_name") or "")
    if not format_is_allowed(format_name):
        raise MediaProcessingError(
            ProcessingErrorCode.UNSUPPORTED, f"input format {format_name or 'unknown'!r} refused"
        )

    streams: Sequence[dict[str, Any]] = data.get("streams") or []
    has_video = any(
        s.get("codec_type") == "video" and not _is_picture(s) for s in streams
    )
    audio = next(
        (s for s in streams if s.get("codec_type") == "audio" and s.get("codec_name")), None
    )
    if audio is None:
        if has_video:
            raise MediaProcessingError(ProcessingErrorCode.NO_AUDIO, "video without an audio track")
        raise MediaProcessingError(ProcessingErrorCode.UNSUPPORTED, "no decodable audio stream")

    return AudioSourceInfo(
        duration=_as_float(audio.get("duration")) or _as_float(fmt.get("duration")),
        audio_codec=str(audio["codec_name"]),
        channels=_as_int(audio.get("channels")),
        sample_rate=_as_int(audio.get("sample_rate")),
        format_name=format_name,
        has_video=has_video,
    )


def parse_voice_output(payload: str) -> VoiceOutputInfo:
    data = _load(payload)
    fmt = data.get("format") or {}
    streams: Sequence[dict[str, Any]] = data.get("streams") or []
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    return VoiceOutputInfo(
        format_name=str(fmt.get("format_name") or ""),
        codec=audio.get("codec_name"),
        channels=_as_int(audio.get("channels")),
        sample_rate=_as_int(audio.get("sample_rate")),
        duration=_as_float(audio.get("duration")) or _as_float(fmt.get("duration")),
    )


def classify_encode_failure(stderr: str) -> ProcessingErrorCode:
    """A damaged source is the user's to fix; anything else is ours."""
    text = (stderr or "").lower()
    if any(marker in text for marker in _DECODE_FAILURE_MARKERS):
        return ProcessingErrorCode.CORRUPT_INPUT
    return ProcessingErrorCode.ENCODE_FAILED


def verify_voice_output(info: VoiceOutputInfo) -> None:
    """Refuse anything Telegram would not show as a voice bubble."""
    if "ogg" not in info.format_name.split(","):
        raise MediaProcessingError(
            ProcessingErrorCode.VERIFY_FAILED, f"voice container is {info.format_name!r}, not ogg"
        )
    if info.codec != "opus":
        raise MediaProcessingError(
            ProcessingErrorCode.VERIFY_FAILED, f"voice codec is {info.codec!r}, not opus"
        )
    if info.duration < _MIN_VOICE_SECONDS:
        raise MediaProcessingError(ProcessingErrorCode.EMPTY_OUTPUT, "voice note has no audio")


def build_voice_ffmpeg_args(
    ffmpeg_bin: str,
    source: Path,
    destination: Path,
    *,
    bitrate: str = VOICE_BITRATE,
    channels: int = VOICE_CHANNELS,
    sample_rate: int = VOICE_SAMPLE_RATE,
) -> list[str]:
    """First audio stream -> mono 48 kHz Opus in OGG, tuned for speech.

    ``-application voip`` is libopus's speech mode. Downmixing to mono and
    resampling are plain format conversions: no filter touches pitch, tempo or
    loudness. Pictures, subtitles, chapters and tags are all dropped - a voice
    note carries sound only.
    """
    return [
        ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-nostdin",
        "-loglevel", "error",
        "-i", str(source),
        "-map", "0:a:0",
        "-vn", "-sn", "-dn",
        "-map_metadata", "-1",
        "-map_chapters", "-1",
        "-ac", str(channels),
        "-ar", str(sample_rate),
        "-c:a", "libopus",
        "-b:a", bitrate,
        "-vbr", "on",
        "-application", "voip",
        "-f", "ogg",
        str(destination),
    ]


class FfmpegVoiceNoteService:
    """ffprobe + ffmpeg implementation of the voice-note conversion."""

    def __init__(self, *, ffmpeg_bin: str, ffprobe_bin: str, timeout: float, probe_timeout: float) -> None:
        self._ffmpeg_bin = ffmpeg_bin
        self._ffprobe_bin = ffprobe_bin
        self._timeout = timeout
        self._probe_timeout = probe_timeout

    async def probe(self, source: Path, *, job_id: str | None = None) -> AudioSourceInfo:
        """Inspect the input; raises ``NO_AUDIO`` / ``UNSUPPORTED`` / ``PROBE_FAILED``."""
        info = parse_audio_source(await self._ffprobe(source, job_id=job_id))
        logger.info(
            "voice source format=%s codec=%s channels=%s rate=%s duration=%.1fs video=%s",
            info.format_name,
            info.audio_codec,
            info.channels,
            info.sample_rate,
            info.duration,
            info.has_video,
            extra={"job_id": job_id or "-"},
        )
        return info

    async def encode(
        self, source: Path, destination: Path, *, job_id: str | None = None
    ) -> VoiceNoteResult:
        args = build_voice_ffmpeg_args(self._ffmpeg_bin, source, destination)
        try:
            await run_command(args, timeout=self._timeout, job_id=job_id)
        except CommandNotFound as exc:
            raise MediaProcessingError(ProcessingErrorCode.TOOL_MISSING, str(exc)) from exc
        except CommandTimeout as exc:
            raise MediaProcessingError(ProcessingErrorCode.TIMEOUT, str(exc)) from exc
        except CommandFailed as exc:
            raise MediaProcessingError(classify_encode_failure(exc.stderr), str(exc)) from exc

        if not destination.exists() or destination.stat().st_size == 0:
            raise MediaProcessingError(ProcessingErrorCode.EMPTY_OUTPUT, "empty voice output")

        output = parse_voice_output(await self._ffprobe(destination, job_id=job_id))
        verify_voice_output(output)

        size = destination.stat().st_size
        logger.info(
            "voice note ready (%d bytes, %.1fs, %s/%s)",
            size,
            output.duration,
            output.format_name,
            output.codec,
            extra={"job_id": job_id or "-"},
        )
        return VoiceNoteResult(
            path=destination, size_bytes=size, filename=destination.name, duration=output.duration
        )

    async def _ffprobe(self, target: Path, *, job_id: str | None) -> str:
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
