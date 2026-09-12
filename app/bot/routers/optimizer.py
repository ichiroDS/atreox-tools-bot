"""Media Optimizer flow: analyse, choose a preset, get a smaller file back.

1. The file is fetched and probed, and the user sees what it is - with a size
   estimate per preset for videos - before anything is encoded. Presets that
   could not make a video meaningfully smaller are not offered; when none
   could, the user is told the file is already well optimized.
2. The chosen preset re-fetches the file (the Bot API server keeps its copy
   between the two steps, exactly like the long-video choice for circles),
   encodes it and sends it back as a File, so Telegram does not recompress it.
   A result that is not really smaller is never sent.
"""

from __future__ import annotations

import enum
import errno
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
from app.bot.callbacks import MenuCallback, OptimizeCallback
from app.bot.errors import busy_message, error_code, user_message
from app.bot.keyboards.common import back_to_menu, main_menu
from app.bot.keyboards.optimizer import optimize_done, preset_choices
from app.bot.media_jobs import (
    ProgressReporter,
    admit,
    commit_early,
    delete_quietly,
    send_media,
)
from app.bot.promo import maybe_send_cta
from app.bot.states import OptimizerStates
from app.config import Settings
from app.db.models import Feature, JobType, User
from app.db.repositories import EventsRepository, JobsRepository
from app.services.jobgate import BusyReason, MediaJobGate, ServerBusy, estimate_job_bytes
from app.services.media.base import MediaProcessingError, ProcessingErrorCode
from app.services.media.optimizer import (
    Analysis,
    MediaOptimizerService,
    OptimizedMedia,
    Preset,
    VideoAnalysis,
    is_worth_sending,
    optimized_filename,
    plan_all,
    plan_video,
)
from app.services.ratelimit import RateLimiter
from app.services.telegram_files import (
    FileKind,
    FileTooLargeError,
    IncomingFile,
    OutputTooLargeError,
    TelegramFileService,
    ensure_sendable,
    extract_optimizer_source,
)
from app.utils.temp_files import job_workspace

logger = logging.getLogger(__name__)

router = Router(name="optimizer")

# Disk on top of the source: the encoded copy, which is never kept when it
# is not smaller than the source - so the source size bounds it.
_OUTPUT_RESERVE_FACTOR = 2.0
_SCRATCH_RESERVE = 16 * 1024 * 1024
# A short clip encodes in seconds; past this even a small file is heavy work.
_LONG_VIDEO_SECONDS = 120

_PRESET_EVENTS = {
    Preset.SMALL: Feature.OPTIMIZE_SMALL,
    Preset.BALANCED: Feature.OPTIMIZE_BALANCED,
    Preset.HIGH: Feature.OPTIMIZE_HIGH,
}

_FAILURE_TEXTS: dict[ProcessingErrorCode, str] = {
    ProcessingErrorCode.UNSUPPORTED: texts.OPTIMIZE_UNSUPPORTED,
    ProcessingErrorCode.PROBE_FAILED: texts.OPTIMIZE_CORRUPT,
    ProcessingErrorCode.CORRUPT_INPUT: texts.OPTIMIZE_CORRUPT,
    ProcessingErrorCode.EMPTY_OUTPUT: texts.OPTIMIZE_CORRUPT,
    ProcessingErrorCode.TIMEOUT: texts.OPTIMIZE_TIMEOUT,
    ProcessingErrorCode.VERIFY_FAILED: texts.OPTIMIZE_VERIFY_FAILED,
    ProcessingErrorCode.DISK_FULL: texts.OPTIMIZE_DISK_FULL,
    ProcessingErrorCode.PROCESS_KILLED: texts.OPTIMIZE_OUT_OF_MEMORY,
    ProcessingErrorCode.SEND_FAILED: texts.OPTIMIZE_SEND_FAILED,
}


class _Outcome(str, enum.Enum):
    OPTIMIZED = "optimized"          # a smaller file was sent
    ALREADY_EFFICIENT = "efficient"  # nothing smaller was worth sending
    BUSY = "busy"
    FAILED = "failed"


# --- entry -------------------------------------------------------------------


@router.callback_query(MenuCallback.filter(F.action == "optimize"))
async def open_optimizer(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession | None = None,
) -> None:
    await state.clear()
    await state.set_state(OptimizerStates.waiting_for_media)
    if session is not None and callback.from_user is not None:
        await EventsRepository(session).record(callback.from_user.id, Feature.MEDIA_OPTIMIZE)
    await _replace(callback, texts.OPTIMIZE_PROMPT, back_to_menu())


# A new file is welcome at any point, including while presets are on screen.
@router.message(StateFilter(OptimizerStates.waiting_for_media, OptimizerStates.choosing_preset))
async def handle_media(
    message: Message,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    session: AsyncSession,
    limiter: RateLimiter | None = None,
    user: User | None = None,
    media_gate: MediaJobGate | None = None,
) -> None:
    incoming = extract_optimizer_source(message)
    if incoming is None:
        await message.answer(texts.OPTIMIZE_UNSUPPORTED, reply_markup=back_to_menu())
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


# --- step 1: analyse ------------------------------------------------------------


async def _analyze(
    *,
    message: Message,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    session: AsyncSession,
    incoming: IncomingFile,
    user_id: int,
    media_gate: MediaJobGate | None = None,
) -> Analysis | None:
    """Fetch and probe, then offer the presets. Returns the analysis, if any."""
    try:
        ticket = admit(
            media_gate, user_id,
            expected_bytes=estimate_job_bytes(incoming.size, extra=_SCRATCH_RESERVE),
            heavy=media_gate is not None and media_gate.is_heavy(incoming.size),
        )
    except ServerBusy as busy:
        await message.answer(_busy_text(busy), reply_markup=back_to_menu())
        return None

    job_id = uuid.uuid4()
    status = await message.answer(texts.OPTIMIZE_ANALYZING)
    files = TelegramFileService.from_settings(bot, settings)
    service = _optimizer_service(settings)
    analysis = None
    offered: list[Preset] = []
    estimates: dict[str, int | None] = {}

    try:
        with ticket:
            async with job_workspace(settings.temp_root, job_id) as workspace:
                fetched = None
                try:
                    fetched = await files.fetch(incoming, workspace, job_id=str(job_id))
                    analysis = await service.analyze(fetched.path, job_id=str(job_id))
                    offered, estimates = _offer(analysis)
                finally:
                    # The Bot API server keeps its copy only while a preset
                    # can still be chosen.
                    if not offered:
                        await files.release(fetched, job_id=str(job_id))
    except Exception as exc:  # noqa: BLE001 - mapped to friendly copy below
        await _record_failure(session, job_id, user_id, incoming, exc)
        await message.answer(_failure_text(exc), reply_markup=back_to_menu())
        await state.set_state(OptimizerStates.waiting_for_media)
        return None
    finally:
        await delete_quietly(status)

    summary = _summary(analysis, incoming)
    if not offered:
        await message.answer(
            f"{summary}\n\n{texts.OPTIMIZE_ALREADY_OPTIMIZED}", reply_markup=optimize_done()
        )
        await state.clear()
        return analysis

    await state.set_state(OptimizerStates.choosing_preset)
    await state.set_data({"optimize_file": incoming.to_dict()})
    await message.answer(
        f"{summary}\n\n{texts.OPTIMIZE_CHOOSE}",
        reply_markup=preset_choices([p.value for p in offered], estimates),
    )
    return analysis


def _offer(analysis: Analysis) -> tuple[list[Preset], dict[str, int | None]]:
    """Presets worth offering, with a size estimate where one is honest.

    Videos are encoded to a target bitrate, so their size is predictable.
    Photos are not estimated: how well a picture compresses depends on what
    it shows, and a number we cannot stand behind is worse than none.
    """
    if not isinstance(analysis, VideoAnalysis):
        return list(Preset), {}
    plans = plan_all(analysis)
    offered = [preset for preset, plan in plans.items() if plan.worthwhile]
    return offered, {preset.value: plans[preset].estimated_bytes for preset in offered}


def _summary(analysis: Analysis, incoming: IncomingFile) -> str:
    if isinstance(analysis, VideoAnalysis):
        text = texts.video_summary(
            width=analysis.width, height=analysis.height, duration=analysis.duration,
            size_bytes=analysis.size_bytes, codec=analysis.video_codec,
            frame_rate=analysis.frame_rate, bitrate=analysis.video_bitrate,
            has_audio=analysis.has_audio,
        )
    else:
        text = texts.image_summary(
            width=analysis.width, height=analysis.height, image_format=analysis.format,
            size_bytes=analysis.size_bytes,
        )
        if analysis.format == "png":
            text += f"\n{texts.OPTIMIZE_PNG_NOTE}"
    if incoming.compressed:
        text += f"\n\n{texts.OPTIMIZE_COMPRESSED_NOTE}"
    return text


# --- step 2: optimise ------------------------------------------------------------


@router.callback_query(
    OptimizerStates.choosing_preset, OptimizeCallback.filter(F.action == "preset")
)
async def handle_preset(
    callback: CallbackQuery,
    callback_data: OptimizeCallback,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    session: AsyncSession,
    user: User | None = None,
    media_gate: MediaJobGate | None = None,
) -> None:
    data = await state.get_data()
    # Leave the choosing state before any round trip, so a second tap cannot
    # start a second job. The data stays, so a failed run can be retried.
    await state.set_state(None)

    incoming = IncomingFile.from_dict(data.get("optimize_file"))
    message = callback.message
    try:
        preset = Preset(callback_data.value)
    except ValueError:
        preset = None
    if incoming is None or preset is None or not isinstance(message, Message):
        await state.clear()
        await callback.answer(texts.OPTIMIZE_CHOICE_EXPIRED, show_alert=True)
        return
    await callback.answer()

    user_id = callback.from_user.id if callback.from_user else 0
    if session is not None:
        await EventsRepository(session).record(user_id, _PRESET_EVENTS[preset])

    outcome = await _run_optimize_job(
        message=message, state=state, bot=bot, settings=settings, session=session,
        incoming=incoming, preset=preset, user_id=user_id, user=user, media_gate=media_gate,
    )
    if outcome in (_Outcome.BUSY, _Outcome.FAILED):
        # The same buttons still work: retry, or pick another preset.
        await state.set_state(OptimizerStates.choosing_preset)


@router.callback_query(OptimizeCallback.filter(F.action == "cancel"))
async def cancel_optimizer(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _replace(callback, texts.MAIN_MENU, main_menu(), notice=texts.CANCELLED)


@router.callback_query(OptimizeCallback.filter(F.action == "preset"))
async def stale_preset(callback: CallbackQuery) -> None:
    """A tap on an old keyboard: the job already ran, or the bot restarted."""
    await callback.answer(texts.OPTIMIZE_CHOICE_EXPIRED, show_alert=True)


def _optimizer_service(settings: Settings) -> MediaOptimizerService:
    return MediaOptimizerService(
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


async def _run_optimize_job(
    *,
    message: Message,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    session: AsyncSession,
    incoming: IncomingFile,
    preset: Preset,
    user_id: int,
    user: User | None = None,
    media_gate: MediaJobGate | None = None,
) -> _Outcome:
    """Fetch, analyse, encode, verify and (if it is smaller) send - always
    cleaning up afterwards."""
    try:
        ticket = admit(
            media_gate, user_id,
            expected_bytes=estimate_job_bytes(
                incoming.size, factor=_OUTPUT_RESERVE_FACTOR, extra=_SCRATCH_RESERVE
            ),
            heavy=_is_heavy(media_gate, incoming),
        )
    except ServerBusy as busy:
        await message.answer(_busy_text(busy), reply_markup=back_to_menu())
        return _Outcome.BUSY

    job_id = uuid.uuid4()
    log_extra = {"job_id": str(job_id)}
    jobs = JobsRepository(session)
    job = None

    async def record_job():
        return await jobs.create(
            job_id=job_id,
            telegram_user_id=user_id,
            job_type=JobType.MEDIA_OPTIMIZE,
            original_filename=incoming.original_filename,
            input_size=incoming.size,
        )

    status = await message.answer(texts.OPTIMIZE_PROCESSING)
    files = TelegramFileService.from_settings(bot, settings)
    service = _optimizer_service(settings)
    before = after = 0

    try:
        with ticket:
            async with job_workspace(settings.temp_root, job_id) as workspace:
                fetched = None
                finished = False
                try:
                    fetched = await files.fetch(incoming, workspace, job_id=str(job_id))
                    analysis = await service.analyze(fetched.path, job_id=str(job_id))
                    before = analysis.size_bytes
                    job = await record_job()
                    await commit_early(session)

                    result = None
                    if not isinstance(analysis, VideoAnalysis) or plan_video(analysis, preset).worthwhile:
                        result = await service.optimize(
                            fetched.path, workspace, analysis, preset,
                            job_id=str(job_id), on_progress=ProgressReporter(status, render=texts.optimize_progress),
                        )

                    if result is not None and is_worth_sending(before, result.size_bytes):
                        ensure_sendable(result.size_bytes, settings.output_limit_bytes)
                        await _send_document(bot, message, result, _output_name(incoming, analysis))
                        after = result.size_bytes
                        await jobs.mark_success(job, output_size=after)
                    else:
                        # Nothing smaller worth having: the original stays the best file.
                        await jobs.mark_success(job, output_size=None)
                    finished = True
                    logger.info(
                        "optimize job %s preset=%s %d -> %s bytes",
                        "sent" if after else "kept original", preset.value, before,
                        after or (result.size_bytes if result else "-"), extra=log_extra,
                    )
                finally:
                    # A failed run keeps the server copy so another preset can
                    # be tried; the server's TTL sweep is the backstop.
                    if finished:
                        await files.release(fetched, job_id=str(job_id))
    except Exception as exc:  # noqa: BLE001 - mapped to friendly copy below
        if job is None:
            job = await record_job()
        await jobs.mark_failed(job, error_code=_error_code(exc))
        _log_failure(exc, log_extra)
        await message.answer(_failure_text(exc), reply_markup=back_to_menu())
        return _Outcome.FAILED
    finally:
        await delete_quietly(status)

    await state.clear()
    if not after:
        await message.answer(texts.OPTIMIZE_ALREADY_COMPRESSED, reply_markup=optimize_done())
        return _Outcome.ALREADY_EFFICIENT
    await message.answer(texts.optimize_done(before, after), reply_markup=optimize_done())
    await maybe_send_cta(message, user)
    return _Outcome.OPTIMIZED


def _output_name(incoming: IncomingFile, analysis: Analysis) -> str:
    image_format = None if isinstance(analysis, VideoAnalysis) else analysis.format
    return optimized_filename(incoming.original_filename, analysis.kind, image_format)


async def _send_document(bot: Bot, message: Message, result: OptimizedMedia, filename: str) -> Any:
    """As a File, with content-type detection off: Telegram must neither turn
    the video into a playable (re-encoded) one nor compress the photo."""
    method = message.answer_document(
        FSInputFile(result.path, filename=filename),
        disable_content_type_detection=True,
    )
    try:
        return await send_media(bot, method, size_bytes=result.size_bytes)
    except TelegramEntityTooLarge as exc:
        raise MediaProcessingError(
            ProcessingErrorCode.OUTPUT_TOO_LARGE, "Bot API refused the upload size"
        ) from exc
    except TelegramAPIError as exc:
        raise MediaProcessingError(ProcessingErrorCode.SEND_FAILED, type(exc).__name__) from exc


# --- failures ----------------------------------------------------------------


def _is_disk_full(error: BaseException) -> bool:
    return isinstance(error, OSError) and error.errno == errno.ENOSPC


def _error_code(error: BaseException) -> str:
    if _is_disk_full(error):
        return ProcessingErrorCode.DISK_FULL.value
    return error_code(error)


def _failure_text(error: BaseException) -> str:
    if _is_disk_full(error):
        return texts.OPTIMIZE_DISK_FULL
    if isinstance(error, MediaProcessingError) and error.code in _FAILURE_TEXTS:
        return _FAILURE_TEXTS[error.code]
    return user_message(error)


def _busy_text(busy: ServerBusy) -> str:
    if busy.reason is BusyReason.DISK:
        return texts.OPTIMIZE_DISK_FULL
    return busy_message(busy)


def _log_failure(error: BaseException, log_extra: dict) -> None:
    if isinstance(error, (MediaProcessingError, FileTooLargeError, OutputTooLargeError)):
        logger.warning("optimize job failed: %s", _error_code(error), extra=log_extra)
    else:
        logger.exception("optimize job failed", extra=log_extra)


async def _record_failure(
    session: AsyncSession, job_id: uuid.UUID, user_id: int, incoming: IncomingFile,
    error: BaseException,
) -> None:
    """A file that could not even be analysed still counts as a failed job."""
    jobs = JobsRepository(session)
    job = await jobs.create(
        job_id=job_id, telegram_user_id=user_id, job_type=JobType.MEDIA_OPTIMIZE,
        original_filename=incoming.original_filename, input_size=incoming.size,
    )
    await jobs.mark_failed(job, error_code=_error_code(error))
    _log_failure(error, {"job_id": str(job_id)})


async def _replace(callback: CallbackQuery, text: str, markup, *, notice: str | None = None) -> None:
    """Edit in place, falling back to a new message when Telegram refuses
    (an unchanged body, or a message too old to edit)."""
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(text, reply_markup=markup)
        except Exception:  # noqa: BLE001 - message may not be editable
            await callback.message.answer(text, reply_markup=markup)
    await callback.answer(notice)
