"""Image -> native Telegram sticker.

The picture is turned into a valid static sticker asset (WEBP, 512 px on the
long side, under Telegram's size ceiling, transparency kept) by a Pillow worker
in its own process, and the result is verified with ffprobe before it is sent.

Only static stickers are made here: no packs are published, nothing is
animated, and no background is invented - an opaque photo stays opaque.
"""

from __future__ import annotations

import enum
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.media.base import MediaProcessingError, ProcessedFile, ProcessingErrorCode
from app.services.media.optimizer import ImageAnalysis, classify_failure, parse_analysis
from app.services.media.probe import build_ffprobe_args
from app.utils.subprocess import (
    CommandFailed,
    CommandNotFound,
    CommandTimeout,
    run_command,
)

logger = logging.getLogger(__name__)

WORKER = Path(__file__).with_name("sticker_worker.py")

# Telegram shows the sticker, not its name, but the extension has to be right.
STICKER_FILENAME = "sticker.webp"

STICKER_SIDE = 512
MAX_STICKER_BYTES = 512 * 1024
MIN_SOURCE_SIDE = 32

# How sticker_worker.py reports why it could not make one.
WORKER_EXIT_CODES = {
    2: ProcessingErrorCode.UNSUPPORTED,
    3: ProcessingErrorCode.TOO_LARGE,
    4: ProcessingErrorCode.CORRUPT_INPUT,
    5: ProcessingErrorCode.TOO_SMALL,
}


class StickerStyle(str, enum.Enum):
    CLEAN = "clean"
    WHITE_OUTLINE = "white_outline"
    BLACK_OUTLINE = "black_outline"


@dataclass(frozen=True)
class StickerImage(ProcessedFile):
    width: int = 0
    height: int = 0
    style: StickerStyle = StickerStyle.CLEAN
    has_alpha: bool = False


def verify_sticker(output: dict[str, Any], size_bytes: int) -> None:
    """A real sticker or nothing: WEBP, 512 on a side, inside the size limit."""

    def fail(detail: str) -> None:
        raise MediaProcessingError(ProcessingErrorCode.VERIFY_FAILED, detail)

    streams = [s for s in output.get("streams") or [] if s.get("codec_type") == "video"]
    if len(streams) != 1 or streams[0].get("codec_name") != "webp":
        fail("the sticker was not written as a sticker image")
    stream = streams[0]
    width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
    if max(width, height) != STICKER_SIDE or min(width, height) > STICKER_SIDE:
        fail(f"a sticker must be {STICKER_SIDE}px on its long side, not {width}x{height}")
    if size_bytes > MAX_STICKER_BYTES:
        fail(f"{size_bytes} bytes is over the sticker limit")


class StickerService:
    def __init__(
        self,
        *,
        ffprobe_bin: str,
        timeout: float,
        probe_timeout: float,
        python_bin: str = sys.executable,
    ) -> None:
        self._ffprobe_bin = ffprobe_bin
        self._timeout = timeout
        self._probe_timeout = probe_timeout
        self._python_bin = python_bin

    async def analyze(self, source: Path, *, job_id: str | None = None) -> ImageAnalysis:
        """A sticker is made from a still picture, and only from one."""
        analysis = parse_analysis(
            await self._probe(source, job_id=job_id), source.stat().st_size
        )
        if not isinstance(analysis, ImageAnalysis):
            raise MediaProcessingError(
                ProcessingErrorCode.UNSUPPORTED, "a sticker is made from a picture"
            )
        return analysis

    async def make(
        self,
        source: Path,
        workspace,
        analysis: ImageAnalysis,
        style: StickerStyle,
        *,
        job_id: str | None = None,
    ) -> StickerImage:
        if min(analysis.width, analysis.height) < MIN_SOURCE_SIDE:
            raise MediaProcessingError(
                ProcessingErrorCode.TOO_SMALL,
                f"{analysis.width}x{analysis.height} is too small for a sticker",
            )
        destination = workspace.new_file(".webp", prefix="stk_")
        request = workspace.path / "sticker.json"
        request.write_text(
            json.dumps({
                "source": str(source),
                "destination": str(destination),
                "format": analysis.format,
                "style": style.value,
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

        if not destination.exists() or destination.stat().st_size == 0:
            raise MediaProcessingError(ProcessingErrorCode.EMPTY_OUTPUT, "empty sticker output")
        size = destination.stat().st_size
        verify_sticker(json.loads(await self._probe(destination, job_id=job_id)), size)

        rendered = json.loads(result.stdout or "{}")
        logger.info(
            "sticker ready %s %sx%s q%s (%d bytes)", style.value, rendered.get("width"),
            rendered.get("height"), rendered.get("quality"), size,
            extra={"job_id": job_id or "-"},
        )
        return StickerImage(
            path=destination,
            size_bytes=size,
            filename=STICKER_FILENAME,
            width=int(rendered.get("width") or 0),
            height=int(rendered.get("height") or 0),
            style=style,
            has_alpha=bool(rendered.get("alpha")),
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
