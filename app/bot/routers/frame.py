"""Extract Frame flow: a video, a moment, a still.

The video is probed once so the wizard knows how long it is - that is what lets
a typed time be checked before anything is decoded - and the frame itself is
only pulled once the format has been chosen. The still goes back as a File, so
Telegram does not recompress the thing the user asked for at full size.
"""

from __future__ import annotations

import enum
import logging
import uuid
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramEntityTooLarge
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.callbacks import FrameCallback, MenuCallback
from app.bot.errors import busy_message, error_code, user_message
from app.bot.keyboards.common import back_to_menu, main_menu
from app.bot.keyboards.frame import format_choices, frame_done, position_choices
from app.bot.media_jobs import admit, commit_early, delete_quietly, send_media
from app.bot.promo import maybe_send_cta
from app.bot.states import FrameStates
from app.config import Settings
from app.db.models import Feature, JobType, User
from app.db.repositories import EventsRepository, JobsRepository
from app.services.jobgate import MediaJobGate, ServerBusy, estimate_job_bytes
from app.services.media.animation import parse_timecode
from app.services.media.base import MediaProcessingError, ProcessingErrorCode
from app.services.media.frame import (
    ExtractedFrame,
    FORMATS,
    FrameService,
    Position,
    frame_filename,
)
from app.services.media.optimizer import VideoAnalysis
from app.services.ratelimit import RateLimiter
from app.services.telegram_files import (
    FileKind,
    FileTooLargeError,
    IncomingFile,
    OutputTooLargeError,
    TelegramFileService,
    ensure_sendable,
    extract_video_source,
)
from app.utils.temp_files import job_workspace

logger = logging.getLogger(__name__)

router = Router(name="frame")

# One still is tiny next to its video; the source dominates the reservation.
_DISK_FACTOR = 1.2
_SCRATCH_RESERVE = 16 * 1024 * 1024
_LONG_VIDEO_SECONDS = 600

_WIZARD_STATES = (
    FrameStates.waiting_for_media,
    FrameStates.choosing_position,
    FrameStates.choosing_format,
)

_FAILURE_TEXTS: dict[ProcessingErrorCode, str] = {
    ProcessingErrorCode.UNSUPPORTED: texts.FRAME_UNSUPPORTED,
    ProcessingErrorCode.PROBE_FAILED: texts.FRAME_CORRUPT,
    ProcessingErrorCode.CORRUPT_INPUT: texts.FRAME_CORRUPT,
    ProcessingErrorCode.EMPTY_OUTPUT: texts.FRAME_NOT_FOUND,
    ProcessingErrorCode.TIMEOUT: texts.FRAME_TIMEOUT,
    ProcessingErrorCode.VERIFY_FAILED: texts.FRAME_VERIFY_FAILED,
    ProcessingErrorCode.DISK_FULL: texts.OPTIMIZE_DISK_FULL,
    ProcessingErrorCode.PROCESS_KILLED: texts.OPTIMIZE_OUT_OF_MEMORY,
    ProcessingErrorCode.SEND_FAILED: texts.OPTIMIZE_SEND_FAILED,
}


class _Outcome(str, enum.Enum):
    DONE = "done"
    BUSY = "busy"
    FAILED = "failed"


# --- entry and the file ---------------------------------------------------------


@router.callback_query(MenuCallback.filter(F.action == "frame"))
async def open_frame(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession | None = None
) -> None:
    await state.clear()
    await state.set_state(FrameStates.waiting_for_media)
    if session is not None and callback.from_user is not None:
        await EventsRepository(session).record(callback.from_user.id, Feature.EXTRACT_FRAME)
    await _replace(callback, texts.FRAME_PROMPT, back_to_menu())


@router.message(StateFilter(*_WIZARD_STATES))
async def handle_media(
    message: Message,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    session: AsyncSession,
    limiter: RateLimiter | None = None,
    media_gate: MediaJobGate | None = None,
) -> None:
    incoming = extract_video_source(message)
    if incoming is None:
        await message.answer(texts.FRAME_UNSUPPORTED, reply_markup=back_to_menu())
        return

    user_id = message.from_user.id if message.from_user else 0
    if limiter is not None and not limiter.allow("media", user_id):
        await message.answer(texts.RATE_LIMITED, reply_markup=back_to_menu())
        return
    if incoming.exceeds(settings.input_limit_bytes):
        # Refused before a single byte is fetched; a smaller file can follow.
        await message.answer(texts.ERROR_TOO_LARGE, reply_markup=back_to_menu())
        return

    await _analyze(
        message=message, state=state, bot=bot, settings=settings, session=session,
        incoming=incoming, user_id=user_id, media_gate=media_gate,
    )


async def _analyze(
    *,
    message: Message,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    session: AsyncSession,
    incoming: IncomingFile,
    user_id: int,
    media_gate: MediaJobGate | None,
) -> None:
    """Probe the video, then offer the positions - the length is needed for
    both the percentages and the range a typed time has to fall inside."""
    try:
        ticket = admit(
            media_gate, user_id,
            expected_bytes=estimate_job_bytes(incoming.size, extra=_SCRATCH_RESERVE),
            heavy=media_gate is not None and media_gate.is_heavy(incoming.size),
        )
    except ServerBusy as busy:
        await message.answer(busy_message(busy), reply_markup=back_to_menu())
        return

    job_id = uuid.uuid4()
    status = await message.answer(texts.ANIMATION_ANALYZING)
    files = TelegramFileService.from_settings(bot, settings)
    service = _frame_service(settings)
    analysis: VideoAnalysis | None = None

    try:
        with ticket:
            async with job_workspace(settings.temp_root, job_id) as workspace:
                fetched = None
                try:
                    fetched = await files.fetch(incoming, workspace, job_id=str(job_id))
                    analysis = await service.analyze(fetched.path, job_id=str(job_id))
                finally:
                    # The server copy is kept only while a choice can still be
                    # made against it.
                    if analysis is None:
                        await files.release(fetched, job_id=str(job_id))
    except Exception as exc:  # noqa: BLE001 - mapped to friendly copy below
        await _record_failure(session, job_id, user_id, incoming, exc)
        await message.answer(_failure_text(exc), reply_markup=back_to_menu())
        await state.set_state(FrameStates.waiting_for_media)
        return
    finally:
        await delete_quietly(status)

    await state.set_state(FrameStates.choosing_position)
    await state.set_data({"file": incoming.to_dict(), "duration": analysis.duration})
    summary = texts.video_summary(
        width=analysis.width, height=analysis.height, duration=analysis.duration,
        size_bytes=analysis.size_bytes, codec=analysis.video_codec,
        frame_rate=analysis.frame_rate, bitrate=analysis.video_bitrate,
        has_audio=analysis.has_audio,
    )
    await message.answer(
        f"{summary}\n\n{texts.FRAME_POSITION_PROMPT}", reply_markup=position_choices()
    )


# --- the moment ------------------------------------------------------------------


@router.callback_query(FrameStates.choosing_position, FrameCallback.filter(F.action == "pos"))
async def handle_position(
    callback: CallbackQuery, callback_data: FrameCallback, state: FSMContext
) -> None:
    try:
        position = Position(callback_data.value)
    except ValueError:
        await callback.answer(texts.ERROR_GENERIC, show_alert=True)
        return

    if position is Position.CUSTOM:
        await state.set_state(FrameStates.typing_time)
        await _replace(callback, texts.FRAME_CUSTOM_TIME_PROMPT, back_to_menu())
        return

    await state.update_data(position=position.value, custom=None)
    await state.set_state(FrameStates.choosing_format)
    await _replace(callback, texts.FRAME_FORMAT_PROMPT, format_choices())


@router.message(FrameStates.typing_time)
async def handle_time(
    message: Message,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    session: AsyncSession,
    limiter: RateLimiter | None = None,
    media_gate: MediaJobGate | None = None,
) -> None:
    if extract_video_source(message) is not None:
        # A second video instead of a time: start over with the new one.
        await handle_media(
            message, state, bot=bot, settings=settings, session=session,
            limiter=limiter, media_gate=media_gate,
        )
        return

    data = await state.get_data()
    duration = float(data.get("duration") or 0)
    seconds = parse_timecode(message.text or "")
    if seconds is None or (duration and seconds > duration):
        await message.answer(texts.FRAME_TIME_REJECTED, reply_markup=back_to_menu())
        return

    await state.update_data(position=Position.CUSTOM.value, custom=seconds)
    await state.set_state(FrameStates.choosing_format)
    await message.answer(texts.FRAME_FORMAT_PROMPT, reply_markup=format_choices())


# --- the format, and the job ------------------------------------------------------


@router.callback_query(FrameStates.choosing_format, FrameCallback.filter(F.action == "format"))
async def handle_format(
    callback: CallbackQuery,
    callback_data: FrameCallback,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    session: AsyncSession,
    user: User | None = None,
    media_gate: MediaJobGate | None = None,
) -> None:
    data = await state.get_data()
    incoming = IncomingFile.from_dict(data.get("file"))
    message = callback.message
    image_format = callback_data.value
    if incoming is None or image_format not in FORMATS or not isinstance(message, Message):
        await state.clear()
        await callback.answer(texts.FRAME_CHOICE_EXPIRED, show_alert=True)
        return
    await callback.answer()

    try:
        position = Position(data.get("position", Position.FIRST.value))
    except ValueError:
        position = Position.FIRST

    # Leave the choosing state so a second tap cannot start a second job.
    await state.set_state(None)
    outcome = await _run_job(
        message=message, state=state, bot=bot, settings=settings, session=session,
        incoming=incoming, position=position, custom=data.get("custom"),
        image_format=image_format, user_id=_user_id(callback), user=user,
        media_gate=media_gate,
    )
    if outcome is not _Outcome.DONE:
        # The format buttons are still on screen: it can simply be tapped again.
        await state.set_state(FrameStates.choosing_format)


@router.callback_query(FrameCallback.filter(F.action == "cancel"))
async def cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _replace(callback, texts.MAIN_MENU, main_menu(), notice=texts.CANCELLED)


@router.callback_query(FrameCallback.filter(F.action == "pos"))
async def stale_position(callback: CallbackQuery) -> None:
    """A tap on an old keyboard: the job already ran, or the bot restarted."""
    await callback.answer(texts.FRAME_CHOICE_EXPIRED, show_alert=True)


@router.callback_query(FrameCallback.filter(F.action == "format"))
async def stale_format(callback: CallbackQuery) -> None:
    await callback.answer(texts.FRAME_CHOICE_EXPIRED, show_alert=True)


def _frame_service(settings: Settings) -> FrameService:
    return FrameService(
        ffmpeg_bin=settings.ffmpeg_bin,
        ffprobe_bin=settings.ffprobe_bin,
        timeout=settings.process_timeout_seconds,
        probe_timeout=min(settings.process_timeout_seconds, 120),
    )


def _is_heavy(media_gate: MediaJobGate | None, incoming: IncomingFile) -> bool:
    if media_gate is None:
        return False
    long_video = incoming.kind is FileKind.VIDEO and (incoming.duration or 0) > _LONG_VIDEO_SECONDS
    return media_gate.is_heavy(incoming.size) or long_video


async def _run_job(
    *,
    message: Message,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    session: AsyncSession,
    incoming: IncomingFile,
    position: Position,
    custom: float | None,
    image_format: str,
    user_id: int,
    user: User | None,
    media_gate: MediaJobGate | None,
) -> _Outcome:
    """Fetch, decode one frame, verify and send - always cleaning up afterwards."""
    try:
        ticket = admit(
            media_gate, user_id,
            expected_bytes=estimate_job_bytes(
                incoming.size, factor=_DISK_FACTOR, extra=_SCRATCH_RESERVE
            ),
            heavy=_is_heavy(media_gate, incoming),
        )
    except ServerBusy as busy:
        await message.answer(busy_message(busy), reply_markup=back_to_menu())
        return _Outcome.BUSY

    job_id = uuid.uuid4()
    log_extra = {"job_id": str(job_id)}
    jobs = JobsRepository(session)
    job = None
    result: ExtractedFrame | None = None

    async def record_job():
        return await jobs.create(
            job_id=job_id,
            telegram_user_id=user_id,
            job_type=JobType.EXTRACT_FRAME,
            original_filename=incoming.original_filename,
            input_size=incoming.size,
        )

    status = await message.answer(texts.FRAME_PROCESSING)
    files = TelegramFileService.from_settings(bot, settings)
    service = _frame_service(settings)

    try:
        with ticket:
            async with job_workspace(settings.temp_root, job_id) as workspace:
                fetched = None
                try:
                    fetched = await files.fetch(incoming, workspace, job_id=str(job_id))
                    analysis = await service.analyze(fetched.path, job_id=str(job_id))
                    job = await record_job()
                    await commit_early(session)

                    result = await service.extract(
                        fetched.path, workspace, analysis, position, image_format,
                        custom=custom, job_id=str(job_id),
                    )
                    ensure_sendable(result.size_bytes, settings.output_limit_bytes)
                    await _send_document(
                        bot, message, result,
                        frame_filename(incoming.original_filename, image_format),
                    )
                    await jobs.mark_success(job, output_size=result.size_bytes)
                    logger.info(
                        "frame job succeeded (%s at %.2fs, %d bytes)",
                        image_format, result.seconds, result.size_bytes, extra=log_extra,
                    )
                finally:
                    await files.release(fetched, job_id=str(job_id))
    except Exception as exc:  # noqa: BLE001 - mapped to friendly copy below
        if job is None:
            job = await record_job()
        await jobs.mark_failed(job, error_code=error_code(exc))
        if isinstance(exc, (MediaProcessingError, FileTooLargeError, OutputTooLargeError)):
            logger.warning("frame job failed: %s", error_code(exc), extra=log_extra)
        else:
            logger.exception("frame job failed", extra=log_extra)
        await message.answer(_failure_text(exc), reply_markup=back_to_menu())
        return _Outcome.FAILED
    finally:
        await delete_quietly(status)

    await state.clear()
    await message.answer(
        texts.frame_done(
            seconds=result.seconds, width=result.width, height=result.height,
            image_format=result.image_format,
        ),
        reply_markup=frame_done(),
    )
    await maybe_send_cta(message, user)
    return _Outcome.DONE


async def _send_document(
    bot: Bot, message: Message, result: ExtractedFrame, filename: str
) -> Any:
    """As a File: a photo upload would be recompressed and resized."""
    method = message.answer_document(
        FSInputFile(result.path, filename=filename),
        disable_content_type_detection=True,
    )
    try:
        return await send_media(bot, method, size_bytes=result.size_bytes)
    except TelegramEntityTooLarge as exc:
        raise MediaProcessingError(
            ProcessingErrorCode.OUTPUT_TOO_LARGE, "the upload was refused as too large"
        ) from exc
    except TelegramAPIError as exc:
        raise MediaProcessingError(ProcessingErrorCode.SEND_FAILED, type(exc).__name__) from exc


def _failure_text(error: BaseException) -> str:
    if isinstance(error, MediaProcessingError) and error.code in _FAILURE_TEXTS:
        return _FAILURE_TEXTS[error.code]
    return user_message(error)


async def _record_failure(
    session: AsyncSession, job_id: uuid.UUID, user_id: int, incoming: IncomingFile,
    error: BaseException,
) -> None:
    jobs = JobsRepository(session)
    job = await jobs.create(
        job_id=job_id, telegram_user_id=user_id, job_type=JobType.EXTRACT_FRAME,
        original_filename=incoming.original_filename, input_size=incoming.size,
    )
    await jobs.mark_failed(job, error_code=error_code(error))
    if isinstance(error, (MediaProcessingError, FileTooLargeError, OutputTooLargeError)):
        logger.warning("frame job failed: %s", error_code(error), extra={"job_id": str(job_id)})
    else:
        logger.exception("frame job failed", extra={"job_id": str(job_id)})


def _user_id(callback: CallbackQuery) -> int:
    return callback.from_user.id if callback.from_user else 0


async def _replace(callback: CallbackQuery, text: str, markup, *, notice: str | None = None) -> None:
    """Edit in place, falling back to a new message when Telegram refuses
    (an unchanged body, or a message too old to edit)."""
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(text, reply_markup=markup)
        except Exception:  # noqa: BLE001 - message may not be editable
            await callback.message.answer(text, reply_markup=markup)
    await callback.answer(notice)
