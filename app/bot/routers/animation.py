"""GIF / MP4 conversion flow.

The file is fetched and probed first, so the buttons offered are the ones that
make sense for what was actually sent: a video can become a GIF or an MP4, a
GIF can become an MP4 or a leaner GIF.

A GIF is only ever a few seconds long - past that the file stops being
shareable - so a long video asks which six seconds to use rather than silently
taking the first ones. The Bot API server keeps its copy between the two steps,
exactly as the optimizer's preset choice does.
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
from app.bot.callbacks import AnimationCallback, MenuCallback
from app.bot.errors import busy_message, error_code, user_message
from app.bot.keyboards.animation import animation_done, clip_choices, conversion_choices
from app.bot.keyboards.common import back_to_menu, main_menu
from app.bot.media_jobs import (
    ProgressReporter,
    admit,
    commit_early,
    delete_quietly,
    send_media,
)
from app.bot.promo import maybe_send_cta
from app.bot.states import AnimationStates
from app.config import Settings
from app.db.models import Feature, JobType, User
from app.db.repositories import EventsRepository, JobsRepository
from app.services.jobgate import MediaJobGate, ServerBusy, estimate_job_bytes
from app.services.media.animation import (
    CLIP_CHOICE_ABOVE_SECONDS,
    GIF_CLIP_SECONDS,
    AnimationService,
    AnimationSource,
    ClipChoice,
    Conversion,
    ConvertedMedia,
    converted_filename,
    parse_timecode,
    plan_clip,
)
from app.services.media.base import MediaProcessingError, ProcessingErrorCode
from app.services.media.optimizer import is_worth_sending
from app.services.ratelimit import RateLimiter
from app.services.telegram_files import (
    FileKind,
    FileTooLargeError,
    IncomingFile,
    OutputTooLargeError,
    TelegramFileService,
    ensure_sendable,
    extract_animation_source,
)
from app.utils.temp_files import job_workspace

logger = logging.getLogger(__name__)

router = Router(name="animation")

# The source plus the converted copy; a GIF can be larger than its video.
_DISK_FACTOR = 3.0
_SCRATCH_RESERVE = 32 * 1024 * 1024
_LONG_VIDEO_SECONDS = 120

_FAILURE_TEXTS: dict[ProcessingErrorCode, str] = {
    ProcessingErrorCode.UNSUPPORTED: texts.ANIMATION_UNSUPPORTED,
    ProcessingErrorCode.PROBE_FAILED: texts.ANIMATION_CORRUPT,
    ProcessingErrorCode.CORRUPT_INPUT: texts.ANIMATION_CORRUPT,
    ProcessingErrorCode.EMPTY_OUTPUT: texts.ANIMATION_CORRUPT,
    ProcessingErrorCode.TIMEOUT: texts.ANIMATION_TIMEOUT,
    ProcessingErrorCode.VERIFY_FAILED: texts.ANIMATION_VERIFY_FAILED,
    ProcessingErrorCode.DISK_FULL: texts.OPTIMIZE_DISK_FULL,
    ProcessingErrorCode.PROCESS_KILLED: texts.OPTIMIZE_OUT_OF_MEMORY,
    ProcessingErrorCode.SEND_FAILED: texts.OPTIMIZE_SEND_FAILED,
}


class _Outcome(str, enum.Enum):
    DONE = "done"
    KEPT_ORIGINAL = "kept_original"   # optimizing the GIF gained nothing
    BUSY = "busy"
    FAILED = "failed"


# --- entry --------------------------------------------------------------------


@router.callback_query(MenuCallback.filter(F.action == "animation"))
async def open_animation(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession | None = None
) -> None:
    await state.clear()
    await state.set_state(AnimationStates.waiting_for_media)
    if session is not None and callback.from_user is not None:
        await EventsRepository(session).record(callback.from_user.id, Feature.CONVERT)
    await _replace(callback, texts.ANIMATION_PROMPT, back_to_menu())


# A new file is welcome at any point of the flow, and starts it over.
@router.message(
    StateFilter(
        AnimationStates.waiting_for_media,
        AnimationStates.choosing_conversion,
        AnimationStates.choosing_clip,
    )
)
async def handle_media(
    message: Message,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    session: AsyncSession,
    limiter: RateLimiter | None = None,
    media_gate: MediaJobGate | None = None,
) -> None:
    incoming = extract_animation_source(message)
    if incoming is None:
        await message.answer(texts.ANIMATION_UNSUPPORTED, reply_markup=back_to_menu())
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
    """Fetch and probe, then offer what this file can become."""
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
    service = _service(settings)
    analysis: AnimationSource | None = None

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
        await state.set_state(AnimationStates.waiting_for_media)
        return
    finally:
        await delete_quietly(status)

    await state.set_state(AnimationStates.choosing_conversion)
    await state.set_data({"file": incoming.to_dict(), "duration": analysis.duration})
    await message.answer(
        f"{_summary(analysis)}\n\n{texts.ANIMATION_CHOOSE}",
        reply_markup=conversion_choices([c.value for c in analysis.conversions]),
    )


def _summary(analysis: AnimationSource) -> str:
    if analysis.is_gif:
        return texts.animation_gif_summary(
            width=analysis.width, height=analysis.height,
            frame_rate=analysis.frame_rate, seconds=analysis.duration,
        )
    return texts.video_summary(
        width=analysis.width, height=analysis.height, duration=analysis.duration,
        size_bytes=analysis.size_bytes, codec=analysis.video_codec,
        frame_rate=analysis.frame_rate, bitrate=0, has_audio=analysis.has_audio,
    )


# --- the choice ----------------------------------------------------------------


@router.callback_query(
    AnimationStates.choosing_conversion, AnimationCallback.filter(F.action == "convert")
)
async def handle_conversion(
    callback: CallbackQuery,
    callback_data: AnimationCallback,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    session: AsyncSession,
    user: User | None = None,
    media_gate: MediaJobGate | None = None,
) -> None:
    data = await state.get_data()
    message = callback.message
    try:
        conversion = Conversion(callback_data.value)
    except ValueError:
        conversion = None
    if conversion is None or not isinstance(message, Message) or not data.get("file"):
        await state.clear()
        await callback.answer(texts.ANIMATION_CHOICE_EXPIRED, show_alert=True)
        return
    await callback.answer()

    duration = float(data.get("duration") or 0)
    if conversion is Conversion.TO_GIF and duration > CLIP_CHOICE_ABOVE_SECONDS:
        # Too long for one GIF: the user picks which part becomes one.
        await state.update_data(conversion=conversion.value)
        await state.set_state(AnimationStates.choosing_clip)
        await message.answer(
            texts.animation_clip_prompt(duration, GIF_CLIP_SECONDS), reply_markup=clip_choices()
        )
        return

    await state.set_state(None)
    await _start(
        message=message, state=state, bot=bot, settings=settings, session=session,
        conversion=conversion, clip_choice=ClipChoice.FIRST, custom_start=None,
        user=user, media_gate=media_gate, user_id=_user_id(callback),
    )


@router.callback_query(AnimationStates.choosing_clip, AnimationCallback.filter(F.action == "clip"))
async def handle_clip(
    callback: CallbackQuery,
    callback_data: AnimationCallback,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    session: AsyncSession,
    user: User | None = None,
    media_gate: MediaJobGate | None = None,
) -> None:
    message = callback.message
    try:
        choice = ClipChoice(callback_data.value)
    except ValueError:
        choice = None
    if choice is None or not isinstance(message, Message):
        await callback.answer(texts.ANIMATION_CHOICE_EXPIRED, show_alert=True)
        return
    await callback.answer()

    if choice is ClipChoice.CUSTOM:
        await state.set_state(AnimationStates.typing_start_time)
        await message.answer(texts.ANIMATION_CUSTOM_TIME_PROMPT, reply_markup=back_to_menu())
        return

    await state.set_state(None)
    await _start(
        message=message, state=state, bot=bot, settings=settings, session=session,
        conversion=Conversion.TO_GIF, clip_choice=choice, custom_start=None,
        user=user, media_gate=media_gate, user_id=_user_id(callback),
    )


@router.message(AnimationStates.typing_start_time)
async def handle_start_time(
    message: Message,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    session: AsyncSession,
    user: User | None = None,
    media_gate: MediaJobGate | None = None,
) -> None:
    data = await state.get_data()
    duration = float(data.get("duration") or 0)
    seconds = parse_timecode(message.text or "")
    if seconds is None or (duration and seconds >= duration):
        await message.answer(texts.ANIMATION_TIME_REJECTED, reply_markup=back_to_menu())
        return

    await state.set_state(None)
    await _start(
        message=message, state=state, bot=bot, settings=settings, session=session,
        conversion=Conversion.TO_GIF, clip_choice=ClipChoice.CUSTOM, custom_start=seconds,
        user=user, media_gate=media_gate,
        user_id=message.from_user.id if message.from_user else 0,
    )


@router.callback_query(AnimationCallback.filter(F.action == "cancel"))
async def cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _replace(callback, texts.MAIN_MENU, main_menu(), notice=texts.CANCELLED)


@router.callback_query(AnimationCallback.filter(F.action == "convert"))
async def stale_conversion(callback: CallbackQuery) -> None:
    """A tap on an old keyboard: the job already ran, or the bot restarted."""
    await callback.answer(texts.ANIMATION_CHOICE_EXPIRED, show_alert=True)


@router.callback_query(AnimationCallback.filter(F.action == "clip"))
async def stale_clip(callback: CallbackQuery) -> None:
    await callback.answer(texts.ANIMATION_CHOICE_EXPIRED, show_alert=True)


# --- the job --------------------------------------------------------------------


async def _start(
    *,
    message: Message,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    session: AsyncSession,
    conversion: Conversion,
    clip_choice: ClipChoice,
    custom_start: float | None,
    user_id: int,
    user: User | None,
    media_gate: MediaJobGate | None,
) -> None:
    data = await state.get_data()
    incoming = IncomingFile.from_dict(data.get("file"))
    if incoming is None:
        await state.clear()
        await message.answer(texts.ANIMATION_CHOICE_EXPIRED, reply_markup=back_to_menu())
        return

    outcome = await _run_job(
        message=message, state=state, bot=bot, settings=settings, session=session,
        incoming=incoming, conversion=conversion, clip_choice=clip_choice,
        custom_start=custom_start, user_id=user_id, user=user, media_gate=media_gate,
    )
    if outcome in (_Outcome.BUSY, _Outcome.FAILED):
        # The same keyboard still works: retry, or pick the other conversion.
        await state.set_state(AnimationStates.choosing_conversion)


def _service(settings: Settings) -> AnimationService:
    return AnimationService(
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
    conversion: Conversion,
    clip_choice: ClipChoice,
    custom_start: float | None,
    user_id: int,
    user: User | None,
    media_gate: MediaJobGate | None,
) -> _Outcome:
    """Fetch, convert, verify and send back - always cleaning up afterwards."""
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
    result: ConvertedMedia | None = None
    sent = False

    async def record_job():
        return await jobs.create(
            job_id=job_id,
            telegram_user_id=user_id,
            job_type=JobType.CONVERT,
            original_filename=incoming.original_filename,
            input_size=incoming.size,
        )

    status = await message.answer(texts.ANIMATION_PROCESSING)
    files = TelegramFileService.from_settings(bot, settings)
    service = _service(settings)

    try:
        with ticket:
            async with job_workspace(settings.temp_root, job_id) as workspace:
                fetched = None
                try:
                    fetched = await files.fetch(incoming, workspace, job_id=str(job_id))
                    analysis = await service.analyze(fetched.path, job_id=str(job_id))
                    job = await record_job()
                    await commit_early(session)

                    progress = ProgressReporter(status, render=texts.animation_progress)
                    if conversion is Conversion.TO_MP4:
                        result = await service.to_mp4(
                            fetched.path, workspace, analysis,
                            job_id=str(job_id), on_progress=progress,
                        )
                    elif conversion is Conversion.OPTIMIZE_GIF:
                        result = await service.optimize_gif(
                            fetched.path, workspace, analysis,
                            job_id=str(job_id), on_progress=progress,
                        )
                    else:
                        clip = plan_clip(analysis, clip_choice, custom_start)
                        result = await service.to_gif(
                            fetched.path, workspace, analysis, clip,
                            job_id=str(job_id), on_progress=progress,
                        )

                    worthwhile = conversion is not Conversion.OPTIMIZE_GIF or is_worth_sending(
                        analysis.size_bytes, result.size_bytes
                    )
                    if worthwhile:
                        ensure_sendable(result.size_bytes, settings.output_limit_bytes)
                        await _send(bot, message, result, _output_name(incoming, result))
                        sent = True
                        await jobs.mark_success(job, output_size=result.size_bytes)
                    else:
                        # A re-encode that saves nothing is not an improvement.
                        await jobs.mark_success(job, output_size=None)
                    logger.info(
                        "convert job %s (%s, %d bytes)",
                        "sent" if sent else "kept original", conversion.value,
                        result.size_bytes, extra=log_extra,
                    )
                finally:
                    await files.release(fetched, job_id=str(job_id))
    except Exception as exc:  # noqa: BLE001 - mapped to friendly copy below
        if job is None:
            job = await record_job()
        await jobs.mark_failed(job, error_code=error_code(exc))
        if isinstance(exc, (MediaProcessingError, FileTooLargeError, OutputTooLargeError)):
            logger.warning("convert job failed: %s", error_code(exc), extra=log_extra)
        else:
            logger.exception("convert job failed", extra=log_extra)
        await message.answer(_failure_text(exc), reply_markup=back_to_menu())
        return _Outcome.FAILED
    finally:
        await delete_quietly(status)

    await state.clear()
    if not sent:
        await message.answer(texts.ANIMATION_GIF_ALREADY_SMALL, reply_markup=animation_done())
        return _Outcome.KEPT_ORIGINAL
    done = texts.ANIMATION_MP4_DONE if conversion is Conversion.TO_MP4 else texts.ANIMATION_GIF_DONE
    await message.answer(done, reply_markup=animation_done())
    await maybe_send_cta(message, user)
    return _Outcome.DONE


def _output_name(incoming: IncomingFile, result: ConvertedMedia) -> str:
    return converted_filename(incoming.original_filename, ".gif" if result.is_gif else ".mp4")


async def _send(bot: Bot, message: Message, result: ConvertedMedia, filename: str) -> Any:
    """A GIF goes as a File; an MP4 goes as an animation when Telegram takes one.

    Telegram plays a ``.gif`` document inline anyway, and sending it as a file
    is what keeps the palette we just built. For the MP4 the animation bubble
    is nicer, but it is not worth failing over - a file is always accepted.
    """
    if result.is_gif:
        return await _try_send(
            bot,
            message.answer_document(
                FSInputFile(result.path, filename=filename),
                disable_content_type_detection=True,
            ),
            result.size_bytes,
        )
    try:
        return await _try_send(
            bot,
            message.answer_animation(
                FSInputFile(result.path, filename=filename),
                width=result.width, height=result.height,
                duration=max(1, round(result.duration)),
            ),
            result.size_bytes,
        )
    except MediaProcessingError as exc:
        if exc.code is not ProcessingErrorCode.SEND_FAILED:
            raise
        logger.info("animation upload refused, sending as a file instead")
        return await _try_send(
            bot,
            message.answer_document(
                FSInputFile(result.path, filename=filename),
                disable_content_type_detection=True,
            ),
            result.size_bytes,
        )


async def _try_send(bot: Bot, method: Any, size_bytes: int) -> Any:
    try:
        return await send_media(bot, method, size_bytes=size_bytes)
    except TelegramEntityTooLarge as exc:
        raise MediaProcessingError(
            ProcessingErrorCode.OUTPUT_TOO_LARGE, "the upload was refused as too large"
        ) from exc
    except TelegramAPIError as exc:
        raise MediaProcessingError(ProcessingErrorCode.SEND_FAILED, type(exc).__name__) from exc


# --- failures --------------------------------------------------------------------


def _failure_text(error: BaseException) -> str:
    if isinstance(error, MediaProcessingError) and error.code in _FAILURE_TEXTS:
        return _FAILURE_TEXTS[error.code]
    return user_message(error)


async def _record_failure(
    session: AsyncSession, job_id: uuid.UUID, user_id: int, incoming: IncomingFile,
    error: BaseException,
) -> None:
    """A file that could not even be probed still counts as a failed job."""
    jobs = JobsRepository(session)
    job = await jobs.create(
        job_id=job_id, telegram_user_id=user_id, job_type=JobType.CONVERT,
        original_filename=incoming.original_filename, input_size=incoming.size,
    )
    await jobs.mark_failed(job, error_code=error_code(error))
    if isinstance(error, (MediaProcessingError, FileTooLargeError, OutputTooLargeError)):
        logger.warning("convert job failed: %s", error_code(error), extra={"job_id": str(job_id)})
    else:
        logger.exception("convert job failed", extra={"job_id": str(job_id)})


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
