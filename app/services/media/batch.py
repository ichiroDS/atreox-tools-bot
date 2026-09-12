"""Batch processing: one tool, applied to each file on its own.

Nothing here re-implements a tool. Each processor is a thin adapter over the
service the single-file flow already uses - ExifTool for Clean Metadata, the
watermark renderer, the optimizer - so a batch cannot drift from what the same
tool does on its own.

Every item is independent: it gets its own subdirectory, its own job row, and
its own outcome. One failure never stops the rest.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.db.models import JobType
from app.services.media.metadata import MetadataService
from app.services.media.optimizer import (
    MediaOptimizerService,
    Preset,
    VideoAnalysis,
    is_worth_sending,
    optimized_filename,
    plan_video,
)
from app.services.media.optimizer import IMAGE_EXTENSIONS
from app.services.media.watermark import (
    LOGO_FILENAME,
    LogoSpec,
    WatermarkService,
    WatermarkSpec,
    watermarked_filename,
)
from app.services.telegram_files import FetchedFile, IncomingFile
from app.utils.temp_files import JobWorkspace

logger = logging.getLogger(__name__)

# Prefixes the batch gives back, so a processed file says what happened to it.
CLEANED_PREFIX = "atreox_cleaned"


class BatchTool(str, enum.Enum):
    CLEAN = "clean"
    WATERMARK = "watermark"
    OPTIMIZE = "optimize"


class ItemStatus(str, enum.Enum):
    SENT = "sent"        # a processed file went back
    SKIPPED = "skipped"  # the original was already the best answer
    FAILED = "failed"


@dataclass(frozen=True)
class ItemOutput:
    """What one processed item produced."""

    path: Path | None
    size_bytes: int
    filename: str
    skipped: bool = False


@dataclass(frozen=True)
class ItemOutcome:
    """What became of one item, for the summary and the logs."""

    index: int
    status: ItemStatus
    filename: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class BatchSummary:
    total: int
    sent: int
    skipped: int
    failed: int
    cancelled: bool = False

    @property
    def successful(self) -> int:
        """A skipped file is a success: its original was already the best one."""
        return self.sent + self.skipped

    @classmethod
    def of(cls, total: int, outcomes: list[ItemOutcome], *, cancelled: bool = False) -> BatchSummary:
        counted = {status: 0 for status in ItemStatus}
        for outcome in outcomes:
            counted[outcome.status] += 1
        return cls(
            total=total,
            sent=counted[ItemStatus.SENT],
            skipped=counted[ItemStatus.SKIPPED],
            failed=counted[ItemStatus.FAILED],
            cancelled=cancelled,
        )


class ItemProcessor(Protocol):
    """One tool, ready to run over each file of a batch."""

    job_type: JobType

    async def run(
        self,
        fetched: FetchedFile,
        workspace: JobWorkspace,
        incoming: IncomingFile,
        *,
        job_id: str | None = None,
    ) -> ItemOutput: ...


class CleanMetadataProcessor:
    """Strips metadata with ExifTool - the Metadata Studio service, unchanged."""

    job_type = JobType.METADATA_CLEAN

    def __init__(self, service: MetadataService) -> None:
        self._service = service

    async def run(self, fetched, workspace, incoming, *, job_id=None) -> ItemOutput:
        if fetched.borrowed:
            # The Bot API server's own file: ExifTool edits in place, so it
            # works on a copy - exactly as the single-file flow does.
            target = workspace.new_file(incoming.extension, prefix="clean_")
            await asyncio.to_thread(shutil.copy2, fetched.path, target)
        else:
            target = fetched.path
        result = await self._service.clean(target, job_id=job_id)
        return ItemOutput(
            path=result.path,
            size_bytes=result.size_bytes,
            filename=incoming.output_filename(prefix=CLEANED_PREFIX),
        )


class WatermarkProcessor:
    """Draws the same watermark on every file, via the Watermark service.

    The mark is either a line of text or a logo. A logo travels as bytes (a
    saved preset holds them in the database) and is written into each item's
    own workspace, so it is cleaned up with everything else.
    """

    job_type = JobType.WATERMARK

    def __init__(
        self,
        service: WatermarkService,
        spec: WatermarkSpec | LogoSpec,
        *,
        logo: bytes | None = None,
        logo_format: str = "png",
    ) -> None:
        self._service = service
        self._spec = spec
        self._logo = logo
        self._logo_format = logo_format

    async def run(self, fetched, workspace, incoming, *, job_id=None) -> ItemOutput:
        analysis = await self._service.analyze(fetched.path, job_id=job_id)
        if isinstance(self._spec, LogoSpec) and self._logo:
            mark = workspace.path / (
                LOGO_FILENAME + IMAGE_EXTENSIONS.get(self._logo_format, ".png")
            )
            mark.write_bytes(self._logo)
            result = await self._service.apply_logo(
                fetched.path, workspace, analysis, self._spec, mark, job_id=job_id
            )
        else:
            result = await self._service.apply(
                fetched.path, workspace, analysis, self._spec, job_id=job_id
            )
        image_format = None if isinstance(analysis, VideoAnalysis) else analysis.format
        return ItemOutput(
            path=result.path,
            size_bytes=result.size_bytes,
            filename=watermarked_filename(incoming.original_filename, analysis.kind, image_format),
        )


class OptimizeProcessor:
    """Runs one optimizer preset over every file, keeping the original when
    the optimized copy would not actually be smaller."""

    job_type = JobType.MEDIA_OPTIMIZE

    def __init__(self, service: MediaOptimizerService, preset: Preset) -> None:
        self._service = service
        self._preset = preset

    async def run(self, fetched, workspace, incoming, *, job_id=None) -> ItemOutput:
        analysis = await self._service.analyze(fetched.path, job_id=job_id)
        image_format = None if isinstance(analysis, VideoAnalysis) else analysis.format
        filename = optimized_filename(incoming.original_filename, analysis.kind, image_format)

        if isinstance(analysis, VideoAnalysis) and not plan_video(analysis, self._preset).worthwhile:
            # Nothing this preset could do would make it smaller.
            return ItemOutput(path=None, size_bytes=0, filename=filename, skipped=True)

        result = await self._service.optimize(
            fetched.path, workspace, analysis, self._preset, job_id=job_id
        )
        if not is_worth_sending(analysis.size_bytes, result.size_bytes):
            logger.info(
                "batch item already efficient (%d -> %d bytes)",
                analysis.size_bytes, result.size_bytes, extra={"job_id": job_id or "-"},
            )
            return ItemOutput(path=None, size_bytes=0, filename=filename, skipped=True)
        return ItemOutput(path=result.path, size_bytes=result.size_bytes, filename=filename)
