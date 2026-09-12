"""Batch Mode: many files, one tool, one at a time.

Collection is forgiving - files arrive one by one or as album items (which
Telegram delivers as separate updates), duplicates are ignored, and an
unsupported file is skipped without disturbing the batch. Only the file
references are remembered; nothing is fetched until processing starts.

Processing is deliberately sequential. The container has little memory, and a
single 4K encode already uses a large part of it, so a batch never starts a
second job of its own - and every item still passes the shared job gate, so
batches from different users cannot pile up either. One item's failure is
recorded and the rest continue.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
import uuid
from typing import Any, Callable

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramEntityTooLarge
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.callbacks import BatchCallback, MenuCallback
from app.bot.errors import error_code
from app.bot.keyboards.batch import (
    batch_done,
    collecting,
    opacity_choices,
    optimizer_choices,
    position_choices,
    size_choices,
    style_choices,
    tool_choices,
    watermark_choices,
)
from app.bot.keyboards.common import back_to_menu, main_menu
from app.bot.media_jobs import admit, commit_early, delete_quietly, send_media
from app.bot.promo import maybe_send_cta
from app.bot.states import BatchStates
from app.config import Settings
from app.db.models import Feature, User
from app.db.repositories import (
    EventsRepository,
    JobsRepository,
    WatermarkPresetsRepository,
)
from app.services.jobgate import MediaJobGate, ServerBusy, estimate_job_bytes
from app.services.media.base import MediaProcessingError, ProcessingErrorCode
from app.services.media.batch import (
    BatchSummary,
    BatchTool,
    CleanMetadataProcessor,
    ItemOutcome,
    ItemProcessor,
    ItemStatus,
    OptimizeProcessor,
    WatermarkProcessor,
)
from app.services.media.metadata import MetadataService
from app.services.media.optimizer import MediaOptimizerService, Preset
from app.services.media.watermark import (
    OPACITIES,
    LogoSpec,
    Position,
    Size,
    Style,
    WatermarkService,
    WatermarkSpec,
    clean_watermark_text,
)
from app.services.ratelimit import RateLimiter
from app.services.telegram_files import (
    FileKind,
    IncomingFile,
    TelegramFileService,
    ensure_sendable,
    extract_optimizer_source,
)
from app.utils.temp_files import BATCH_PREFIX, JobWorkspace, job_workspace

logger = logging.getLogger(__name__)

router = Router(name="batch")

# Scratch space one item may need beyond its own size.
_ITEM_SCRATCH = 16 * 1024 * 1024
_ITEM_DISK_FACTOR = 2.0
_LONG_VIDEO_SECONDS = 120
# When the server is busy, an item waits rather than failing straight away.
_BUSY_RETRIES = 2
_BUSY_DELAY_SECONDS = 5.0

# Steps where a late album item still belongs to the batch being built. Files
# that arrive after "Done Uploading" join the same batch instead of being lost.
_COLLECTING_STATES = (
    BatchStates.collecting,
    BatchStates.choosing_tool,
    BatchStates.choosing_watermark,
    BatchStates.typing_watermark_text,
    BatchStates.choosing_position,
    BatchStates.choosing_style,
    BatchStates.choosing_size,
    BatchStates.choosing_opacity,
    BatchStates.choosing_optimizer_preset,
)


# --- entry and collection ------------------------------------------------------


@router.callback_query(MenuCallback.filter(F.action == "batch"))
async def open_batch(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession | None = None
) -> None:
    await state.clear()
    await state.set_state(BatchStates.collecting)
    await state.set_data({"files": []})
    await _replace(callback, texts.BATCH_PROMPT, collecting())


@router.message(BatchStates.typing_watermark_text)
async def handle_watermark_text(
    message: Message,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    limiter: RateLimiter | None = None,
) -> None:
    if extract_optimizer_source(message) is not None:
        # A file, not the text: it belongs to the batch.
        await collect_file(message, state, bot=bot, settings=settings, limiter=limiter)
        return

    text = clean_watermark_text(message.text or message.caption)
    if text is None:
        await message.answer(texts.WATERMARK_TEXT_REJECTED, reply_markup=back_to_menu())
        return
    await state.update_data(text=text)
    await state.set_state(BatchStates.choosing_position)
    await message.answer(texts.WATERMARK_POSITION_PROMPT, reply_markup=position_choices())


@router.message(StateFilter(*_COLLECTING_STATES))
async def collect_file(
    message: Message,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    limiter: RateLimiter | None = None,
) -> None:
    """Add one file to the batch. Album items land here one update at a time."""
    incoming = extract_optimizer_source(message)
    if incoming is None:
        await message.answer(texts.BATCH_UNSUPPORTED)
        return

    user_id = message.from_user.id if message.from_user else 0
    # A roomy bucket: collecting is cheap, and an album of 20 arrives at once.
    if limiter is not None and not limiter.allow("batch_upload", user_id):
        await message.answer(texts.RATE_LIMITED, reply_markup=back_to_menu())
        return
    if incoming.exceeds(settings.input_limit_bytes):
        await message.answer(texts.ERROR_TOO_LARGE)
        return

    data = await state.get_data()
    files: list[dict] = list(data.get("files") or [])
    if any(existing.get("file_id") == incoming.file_id for existing in files):
        # Telegram re-delivered an album item: it is already in the batch.
        return
    if len(files) >= settings.max_batch_files:
        await message.answer(texts.batch_full(settings.max_batch_files))
        return

    files.append(incoming.to_dict())
    await state.update_data(files=files)
    await _show_collected(message, bot, state, len(files), settings.max_batch_files)


async def _show_collected(
    message: Message, bot: Bot, state: FSMContext, count: int, maximum: int
) -> None:
    """One message, edited as the batch grows - never one message per file."""
    data = await state.get_data()
    text = texts.batch_collected(count, maximum)
    status_id = data.get("status_id")
    if status_id:
        try:
            await bot.edit_message_text(
                chat_id=message.chat.id, message_id=status_id, text=text,
                reply_markup=collecting(),
            )
            return
        except Exception:  # noqa: BLE001 - deleted, too old, or unchanged
            pass
    sent = await message.answer(text, reply_markup=collecting())
    await state.update_data(status_id=getattr(sent, "message_id", None))


@router.message(BatchStates.processing)
async def busy_with_a_batch(message: Message) -> None:
    await message.answer(texts.BATCH_RUNNING)


# --- choosing what to do -------------------------------------------------------


@router.callback_query(BatchCallback.filter(F.action == "done"))
async def done_uploading(callback: CallbackQuery, state: FSMContext) -> None:
    files = (await state.get_data()).get("files") or []
    if not files:
        await callback.answer(texts.BATCH_EMPTY, show_alert=True)
        return
    await state.set_state(BatchStates.choosing_tool)
    await _replace(callback, texts.BATCH_CHOOSE_TOOL, tool_choices())


@router.callback_query(BatchStates.choosing_tool, BatchCallback.filter(F.action == "tool"))
async def choose_tool(
    callback: CallbackQuery,
    callback_data: BatchCallback,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    session: AsyncSession,
    limiter: RateLimiter | None = None,
    user: User | None = None,
    media_gate: MediaJobGate | None = None,
) -> None:
    try:
        tool = BatchTool(callback_data.value)
    except ValueError:
        await callback.answer(texts.ERROR_GENERIC, show_alert=True)
        return

    if tool is BatchTool.OPTIMIZE:
        await state.set_state(BatchStates.choosing_optimizer_preset)
        await _replace(callback, texts.BATCH_OPTIMIZE_PROMPT, optimizer_choices())
        return
    if tool is BatchTool.WATERMARK:
        presets = await _presets(session, _user_id(callback))
        await state.set_state(BatchStates.choosing_watermark)
        await _replace(callback, texts.BATCH_WATERMARK_PROMPT, watermark_choices(presets))
        return

    await callback.answer()
    await _start(
        callback, state, bot=bot, settings=settings, session=session,
        processor=CleanMetadataProcessor(
            MetadataService(
                exiftool_bin=settings.exiftool_path, timeout=settings.process_timeout_seconds
            )
        ),
        limiter=limiter, user=user, media_gate=media_gate,
    )


@router.callback_query(
    BatchStates.choosing_optimizer_preset, BatchCallback.filter(F.action == "preset")
)
async def choose_optimizer_preset(
    callback: CallbackQuery,
    callback_data: BatchCallback,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    session: AsyncSession,
    limiter: RateLimiter | None = None,
    user: User | None = None,
    media_gate: MediaJobGate | None = None,
) -> None:
    try:
        preset = Preset(callback_data.value)
    except ValueError:
        await callback.answer(texts.ERROR_GENERIC, show_alert=True)
        return
    await callback.answer()
    await _start(
        callback, state, bot=bot, settings=settings, session=session,
        processor=OptimizeProcessor(_optimizer_service(settings), preset),
        limiter=limiter, user=user, media_gate=media_gate,
    )


@router.callback_query(BatchStates.choosing_watermark, BatchCallback.filter(F.action == "preset"))
async def choose_watermark_preset(
    callback: CallbackQuery,
    callback_data: BatchCallback,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    session: AsyncSession,
    limiter: RateLimiter | None = None,
    user: User | None = None,
    media_gate: MediaJobGate | None = None,
) -> None:
    preset = None
    if session is not None and callback_data.value.isdigit():
        preset = await WatermarkPresetsRepository(session).get(
            int(callback_data.value), _user_id(callback)
        )
    if preset is None:
        await callback.answer(texts.WATERMARK_PRESET_GONE, show_alert=True)
        return

    if preset.is_logo:
        # A saved logo brands the whole batch, image by image.
        logo_spec = LogoSpec.from_dict({
            "kind": "logo", "position": preset.position,
            "size": preset.size, "opacity": preset.opacity,
        })
        await callback.answer()
        await _start(
            callback, state, bot=bot, settings=settings, session=session,
            processor=WatermarkProcessor(
                _watermark_service(settings), logo_spec,
                logo=preset.logo, logo_format=preset.logo_format or "png",
            ),
            limiter=limiter, user=user, media_gate=media_gate,
        )
        return

    spec = WatermarkSpec.from_dict({
        "text": preset.text, "position": preset.position, "style": preset.style,
        "size": preset.size, "opacity": preset.opacity,
    })
    await callback.answer()
    await _start(
        callback, state, bot=bot, settings=settings, session=session,
        processor=WatermarkProcessor(_watermark_service(settings), spec),
        limiter=limiter, user=user, media_gate=media_gate,
    )


@router.callback_query(BatchStates.choosing_watermark, BatchCallback.filter(F.action == "new"))
async def ask_for_watermark_text(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BatchStates.typing_watermark_text)
    await _replace(callback, texts.WATERMARK_TYPE_PROMPT, back_to_menu())


# The look is chosen once and used for every file in the batch.
@router.callback_query(BatchStates.choosing_position, BatchCallback.filter(F.action == "pos"))
async def choose_position(
    callback: CallbackQuery, callback_data: BatchCallback, state: FSMContext
) -> None:
    if not _valid(Position, callback_data.value):
        await callback.answer(texts.ERROR_GENERIC, show_alert=True)
        return
    await state.update_data(position=callback_data.value)
    await state.set_state(BatchStates.choosing_style)
    await _replace(callback, texts.WATERMARK_STYLE_PROMPT, style_choices())


@router.callback_query(BatchStates.choosing_style, BatchCallback.filter(F.action == "style"))
async def choose_style(
    callback: CallbackQuery, callback_data: BatchCallback, state: FSMContext
) -> None:
    if not _valid(Style, callback_data.value):
        await callback.answer(texts.ERROR_GENERIC, show_alert=True)
        return
    await state.update_data(style=callback_data.value)
    await state.set_state(BatchStates.choosing_size)
    await _replace(callback, texts.WATERMARK_SIZE_PROMPT, size_choices())


@router.callback_query(BatchStates.choosing_size, BatchCallback.filter(F.action == "size"))
async def choose_size(
    callback: CallbackQuery, callback_data: BatchCallback, state: FSMContext
) -> None:
    if not _valid(Size, callback_data.value):
        await callback.answer(texts.ERROR_GENERIC, show_alert=True)
        return
    await state.update_data(size=callback_data.value)
    await state.set_state(BatchStates.choosing_opacity)
    await _replace(callback, texts.WATERMARK_OPACITY_PROMPT, opacity_choices(OPACITIES))


@router.callback_query(BatchStates.choosing_opacity, BatchCallback.filter(F.action == "opacity"))
async def choose_opacity(
    callback: CallbackQuery,
    callback_data: BatchCallback,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    session: AsyncSession,
    limiter: RateLimiter | None = None,
    user: User | None = None,
    media_gate: MediaJobGate | None = None,
) -> None:
    if not callback_data.value.isdigit() or int(callback_data.value) not in OPACITIES:
        await callback.answer(texts.ERROR_GENERIC, show_alert=True)
        return
    data = await state.get_data()
    spec = WatermarkSpec.from_dict({
        "text": data.get("text"),
        "position": data.get("position", Position.BOTTOM_RIGHT.value),
        "style": data.get("style", Style.WHITE_SHADOW.value),
        "size": data.get("size", Size.M.value),
        "opacity": int(callback_data.value),
    })
    if spec is None:
        await state.clear()
        await callback.answer(texts.WATERMARK_CHOICE_EXPIRED, show_alert=True)
        return
    await callback.answer()
    await _start(
        callback, state, bot=bot, settings=settings, session=session,
        processor=WatermarkProcessor(_watermark_service(settings), spec),
        limiter=limiter, user=user, media_gate=media_gate,
    )


@router.callback_query(BatchCallback.filter(F.action == "cancel"))
async def cancel_batch(callback: CallbackQuery, state: FSMContext) -> None:
    if await state.get_state() == BatchStates.processing.state:
        # Mid-run: stop after the file currently being processed.
        await state.update_data(cancelled=True)
        await callback.answer(texts.CANCELLED)
        return
    await state.clear()
    await _replace(callback, texts.MAIN_MENU, main_menu(), notice=texts.CANCELLED)


def _valid(enum_type: type, value: str) -> bool:
    try:
        enum_type(value)
    except ValueError:
        return False
    return True


async def _presets(session: AsyncSession | None, user_id: int) -> list:
    if session is None:
        return []
    return await WatermarkPresetsRepository(session).list_for(user_id)


def _user_id(callback: CallbackQuery) -> int:
    return callback.from_user.id if callback.from_user else 0


def _optimizer_service(settings: Settings) -> MediaOptimizerService:
    return MediaOptimizerService(
        ffmpeg_bin=settings.ffmpeg_bin, ffprobe_bin=settings.ffprobe_bin,
        timeout=settings.process_timeout_seconds,
        probe_timeout=min(settings.process_timeout_seconds, 120),
    )


def _watermark_service(settings: Settings) -> WatermarkService:
    return WatermarkService(
        ffmpeg_bin=settings.ffmpeg_bin, ffprobe_bin=settings.ffprobe_bin,
        timeout=settings.process_timeout_seconds,
        probe_timeout=min(settings.process_timeout_seconds, 120),
        font=settings.watermark_font,
    )


# --- running the batch ----------------------------------------------------------


async def _start(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    bot: Bot,
    settings: Settings,
    session: AsyncSession,
    processor: ItemProcessor,
    limiter: RateLimiter | None = None,
    user: User | None = None,
    media_gate: MediaJobGate | None = None,
) -> BatchSummary | None:
    message = callback.message
    if not isinstance(message, Message):
        await state.clear()
        return None
    user_id = _user_id(callback)
    # One fair-use check for the whole run, never per file.
    if limiter is not None and not limiter.allow("media", user_id):
        await message.answer(texts.RATE_LIMITED, reply_markup=back_to_menu())
        return None
    return await run_batch(
        message=message, state=state, bot=bot, settings=settings, session=session,
        processor=processor, user_id=user_id, user=user, media_gate=media_gate,
    )


async def run_batch(
    *,
    message: Message,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    session: AsyncSession,
    processor: ItemProcessor,
    user_id: int,
    user: User | None = None,
    media_gate: MediaJobGate | None = None,
) -> BatchSummary:
    """Process every collected file, one at a time, and report what happened."""
    data = await state.get_data()
    files = [IncomingFile.from_dict(raw) for raw in data.get("files") or []]
    files = [incoming for incoming in files if incoming is not None]
    total = len(files)

    await state.set_state(BatchStates.processing)
    await state.update_data(cancelled=False)
    await _record(session, user_id, Feature.BATCH_STARTED)
    await commit_early(session)

    batch_id = uuid.uuid4()
    log_extra = {"job_id": f"batch-{batch_id}"}
    logger.info("batch started: %d file(s), %s", total, processor.job_type.value, extra=log_extra)

    status = await message.answer(texts.batch_progress(0, total))
    progress = BatchProgress(status, total)
    outcomes: list[ItemOutcome] = []
    cancelled = False

    async with job_workspace(settings.temp_root, batch_id, prefix=BATCH_PREFIX) as batch_workspace:
        for index, incoming in enumerate(files, start=1):
            if (await state.get_data()).get("cancelled"):
                cancelled = True
                logger.info("batch cancelled after %d item(s)", len(outcomes), extra=log_extra)
                break
            outcomes.append(await _process_item(
                index=index, incoming=incoming, processor=processor,
                batch_workspace=batch_workspace, message=message, bot=bot,
                settings=settings, session=session, user_id=user_id, media_gate=media_gate,
            ))
            await progress.update(len(outcomes))

    await delete_quietly(status)
    summary = BatchSummary.of(total, outcomes, cancelled=cancelled)
    await _record(session, user_id, Feature.BATCH_COMPLETED)
    logger.info(
        "batch finished: %d sent, %d skipped, %d failed, cancelled=%s",
        summary.sent, summary.skipped, summary.failed, cancelled, extra=log_extra,
    )

    await state.clear()
    await message.answer(
        texts.batch_summary(
            total=summary.total, successful=summary.successful, failed=summary.failed,
            skipped=summary.skipped, cancelled=cancelled,
        ),
        reply_markup=batch_done(),
    )
    if summary.sent:
        await maybe_send_cta(message, user)
    return summary


async def _process_item(
    *,
    index: int,
    incoming: IncomingFile,
    processor: ItemProcessor,
    batch_workspace: JobWorkspace,
    message: Message,
    bot: Bot,
    settings: Settings,
    session: AsyncSession,
    user_id: int,
    media_gate: MediaJobGate | None,
) -> ItemOutcome:
    """One file: fetch, process, send. Its failure stays its own."""
    item_path = batch_workspace.path / f"item_{index:03d}"
    await asyncio.to_thread(item_path.mkdir, parents=True, exist_ok=True)
    workspace = JobWorkspace(job_id=uuid.uuid4(), path=item_path)
    job_id = str(workspace.job_id)
    log_extra = {"job_id": job_id}
    jobs = JobsRepository(session)
    files = TelegramFileService.from_settings(bot, settings)
    job = None

    try:
        ticket = await _admit_item(media_gate, user_id, incoming)
    except ServerBusy:
        logger.warning("batch item %d refused: the server stayed busy", index, extra=log_extra)
        return ItemOutcome(index=index, status=ItemStatus.FAILED, error_code="server_busy")

    try:
        with ticket:
            fetched = None
            try:
                job = await jobs.create(
                    job_id=workspace.job_id, telegram_user_id=user_id,
                    job_type=processor.job_type,
                    original_filename=incoming.original_filename, input_size=incoming.size,
                )
                await commit_early(session)

                fetched = await files.fetch(incoming, workspace, job_id=job_id)
                output = await processor.run(fetched, workspace, incoming, job_id=job_id)

                if output.path is None:
                    # Already the best version of itself: the original stays.
                    await jobs.mark_success(job, output_size=None)
                    return ItemOutcome(
                        index=index, status=ItemStatus.SKIPPED, filename=output.filename
                    )

                ensure_sendable(output.size_bytes, settings.output_limit_bytes)
                await _send_document(bot, message, output.path, output.filename, output.size_bytes)
                await jobs.mark_success(job, output_size=output.size_bytes)
                return ItemOutcome(index=index, status=ItemStatus.SENT, filename=output.filename)
            finally:
                await files.release(fetched, job_id=job_id)
    except Exception as exc:  # noqa: BLE001 - one item's failure, not the batch's
        code = error_code(exc)
        if job is not None:
            await jobs.mark_failed(job, error_code=code)
        if isinstance(exc, MediaProcessingError):
            logger.warning("batch item %d failed: %s", index, code, extra=log_extra)
        else:
            logger.exception("batch item %d failed", index, extra=log_extra)
        return ItemOutcome(index=index, status=ItemStatus.FAILED, error_code=code)
    finally:
        # Free the item's disk before the next one starts.
        await asyncio.to_thread(shutil.rmtree, item_path, True)


async def _admit_item(gate: MediaJobGate | None, user_id: int, incoming: IncomingFile):
    """Wait out a busy server for a moment rather than failing the item."""
    for attempt in range(_BUSY_RETRIES + 1):
        try:
            return admit(
                gate, user_id,
                expected_bytes=estimate_job_bytes(
                    incoming.size, factor=_ITEM_DISK_FACTOR, extra=_ITEM_SCRATCH
                ),
                heavy=_is_heavy(gate, incoming),
            )
        except ServerBusy:
            if attempt == _BUSY_RETRIES:
                raise
            await asyncio.sleep(_BUSY_DELAY_SECONDS)
    raise ServerBusy  # pragma: no cover - the loop returns or raises above


def _is_heavy(media_gate: MediaJobGate | None, incoming: IncomingFile) -> bool:
    if media_gate is None:
        return False
    long_video = incoming.kind is FileKind.VIDEO and (incoming.duration or 0) > _LONG_VIDEO_SECONDS
    return media_gate.is_heavy(incoming.size) or long_video


async def _send_document(
    bot: Bot, message: Message, path, filename: str, size_bytes: int
) -> Any:
    """As a File, so Telegram passes the processed bytes through untouched."""
    method = message.answer_document(
        FSInputFile(path, filename=filename), disable_content_type_detection=True
    )
    try:
        return await send_media(bot, method, size_bytes=size_bytes)
    except TelegramEntityTooLarge as exc:
        raise MediaProcessingError(
            ProcessingErrorCode.OUTPUT_TOO_LARGE, "Bot API refused the upload size"
        ) from exc
    except TelegramAPIError as exc:
        raise MediaProcessingError(ProcessingErrorCode.SEND_FAILED, type(exc).__name__) from exc


class BatchProgress:
    """One progress message, edited - never one message per file."""

    def __init__(
        self,
        status: Any,
        total: int,
        *,
        min_interval: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._status = status
        self._total = total
        self._min_interval = min_interval
        self._clock = clock
        self._last = 0.0

    async def update(self, done: int) -> None:
        now = self._clock()
        if done < self._total and now - self._last < self._min_interval:
            return
        self._last = now
        try:
            await self._status.edit_text(texts.batch_progress(done, self._total))
        except Exception:  # noqa: BLE001 - progress is cosmetic
            pass


async def _record(session: AsyncSession | None, user_id: int, feature: Feature) -> None:
    if session is not None and user_id:
        await EventsRepository(session).record(user_id, feature)


async def _replace(callback: CallbackQuery, text: str, markup, *, notice: str | None = None) -> None:
    """Edit in place, falling back to a new message when Telegram refuses
    (an unchanged body, or a message too old to edit)."""
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(text, reply_markup=markup)
        except Exception:  # noqa: BLE001 - message may not be editable
            await callback.message.answer(text, reply_markup=markup)
    await callback.answer(notice)
