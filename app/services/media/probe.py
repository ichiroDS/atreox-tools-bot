"""ffprobe wrapper."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Sequence

from app.services.media.base import MediaInfo, MediaProcessingError, ProcessingErrorCode
from app.utils.subprocess import (
    CommandFailed,
    CommandNotFound,
    CommandTimeout,
    run_command,
)

logger = logging.getLogger(__name__)


def build_ffprobe_args(ffprobe_bin: str, source: Path) -> list[str]:
    """Argument array for a JSON stream/format dump. No shell, ever."""
    return [
        ffprobe_bin,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        "--",
        str(source),
    ]


def _stream_rotation(stream: dict[str, Any]) -> int:
    tags = stream.get("tags") or {}
    raw = tags.get("rotate")
    if raw is None:
        for side_data in stream.get("side_data_list") or []:
            if "rotation" in side_data:
                raw = side_data["rotation"]
                break
    try:
        rotation = int(float(raw))
    except (TypeError, ValueError):
        return 0
    return abs(rotation) % 360


def parse_probe_output(payload: str) -> MediaInfo:
    """Turn ffprobe JSON into a :class:`MediaInfo`."""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise MediaProcessingError(ProcessingErrorCode.PROBE_FAILED, "invalid ffprobe json") from exc

    streams: Sequence[dict[str, Any]] = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise MediaProcessingError(ProcessingErrorCode.UNSUPPORTED, "no video stream")

    duration_raw = video.get("duration") or (data.get("format") or {}).get("duration") or 0
    try:
        duration = float(duration_raw)
    except (TypeError, ValueError):
        duration = 0.0

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    if width <= 0 or height <= 0:
        raise MediaProcessingError(ProcessingErrorCode.PROBE_FAILED, "missing dimensions")

    return MediaInfo(
        width=width,
        height=height,
        duration=max(0.0, duration),
        rotation=_stream_rotation(video),
        video_codec=video.get("codec_name"),
        audio_codec=audio.get("codec_name") if audio else None,
    )


class MediaProbe:
    """Async ffprobe front-end."""

    def __init__(self, ffprobe_bin: str, *, timeout: float = 30.0) -> None:
        self._ffprobe_bin = ffprobe_bin
        self._timeout = timeout

    async def probe(self, source: Path, *, job_id: str | None = None) -> MediaInfo:
        args = build_ffprobe_args(self._ffprobe_bin, source)
        try:
            result = await run_command(args, timeout=self._timeout, job_id=job_id)
        except CommandNotFound as exc:
            raise MediaProcessingError(ProcessingErrorCode.TOOL_MISSING, str(exc)) from exc
        except CommandTimeout as exc:
            raise MediaProcessingError(ProcessingErrorCode.TIMEOUT, str(exc)) from exc
        except CommandFailed as exc:
            raise MediaProcessingError(ProcessingErrorCode.PROBE_FAILED, str(exc)) from exc
        return parse_probe_output(result.stdout)
