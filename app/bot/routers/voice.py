"""Audio / video -> Telegram voice message flow.

Any audio file, or the sound track of a video, comes back as a native voice
message (``sendVoice``, OGG/Opus), so it plays as a waveform bubble rather
than a music track or a file.
"""

from __future__ import annotations

import enum
import logging
import uuid
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramEntityTooLarge
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.callbacks import MenuCallback, VoiceCallback
from app.bot.errors import busy_message, error_code, user_message
from app.bot.keyboards.common import back_to_menu
from app.bot.keyboards.voice import voice_done, voice_forward_help
from app.bot.media_jobs import admit, commit_early, delete_quietly, send_media
from app.bot.promo import maybe_send_cta
from app.bot.states import VoiceStates
from app.config import Settings
from app.db.models import Feature, JobType, User
from app.db.repositories import EventsRepository, JobsRepository
from app.services.jobgate import MediaJobGate, ServerBusy, estimate_job_bytes
from app.services.media.base import MediaProcessingError, ProcessingErrorCode
from app.services.media.voice import FfmpegVoiceNoteService, VoiceNoteResult
from app.services.ratelimit import RateLimiter
from app.services.telegram_files import (
    FileTooLargeError,
    IncomingFile,
    OutputTooLargeError,
    TelegramFileService,
    ensure_sendable,
    extract_voice_source,
)
from app.utils.temp_files import job_workspace

logger = logging.getLogger(__name__)

router = Router(name="voice")

# Disk a voice job needs on top of its source. Opus at 48 kbps is ~21 MB per
# hour, so this covers a very long recording.
_VOICE_OUTPUT_RESERVE = 64 * 1024 * 1024
# A small file can still hold hours of audio; past this it counts as heavy.
_LONG_SOURCE_SECONDS = 30 * 60

# The upload's name matters: Telegram picks the voice renderer from an
# OGG/Opus upload, and the Bot API server derives its MIME type from the name.
VOICE_UPLOAD_NAME = "voice.ogg"

# How Telegram reports a user whose privacy settings refuse voice messages.
_VOICE_FORBIDDEN_MARKER = "VOICE_MESSAGES_FORBIDDEN"

_FAILURE_TEXTS: dict[ProcessingErrorCode, str] = {
    ProcessingErrorCode.NO_AUDIO: texts.VOICE_NO_AUDIO,
    ProcessingErrorCode.UNSUPPORTED: texts.VOICE_UNSUPPORTED,
    ProcessingErrorCode.PROBE_FAILED: texts.VOICE_CORRUPT,
    ProcessingErrorCode.CORRUPT_INPUT: texts.VOICE_CORRUPT,
    ProcessingErrorCode.EMPTY_OUTPUT: texts.VOICE_CORRUPT,
    ProcessingErrorCode.TIMEOUT: texts.VOICE_TIMEOUT,
    ProcessingErrorCode.SEND_FAILED: texts.VOICE_SEND_FAILED,
    ProcessingErrorCode.VOICE_FORBIDDEN: texts.VOICE_FORBIDDEN,
}


class _Outcome(str, enum.Enum):
    DONE = "done"
    BUSY = "busy"
    FAILED = "failed"


@router.callback_query(MenuCallback.filter(F.action == "voice"))
async def open_voice(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession | None = None,
) -> None:
    await state.set_state(VoiceStates.waiting_for_media)
    if session is not None and callback.from_user is not None:
        await EventsRepository(session).record(callback.from_user.id, Feature.VOICE_NOTE)
    await _replace(callback, texts.VOICE_PROMPT, back_to_menu())


@router.message(VoiceStates.waiting_for_media)
async def handle_voice_source(
    message: Message,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    session: AsyncSession,
    limiter: RateLimiter | None = None,
    user: User | None = None,
    media_gate: MediaJobGate | None = None,
) -> None:
    incoming = extract_voice_source(message)
    if incoming is None:
        await message.answer(texts.VOICE_UNSUPPORTED, reply_markup=back_to_menu())
        return

    user_id = message.from_user.id if message.from_user else 0
    if limiter is not None and not limiter.allow("media", user_id):
        # The state is kept so the user can simply resend in a moment.
        await message.answer(texts.RATE_LIMITED, reply_markup=back_to_menu())
        return

    if incoming.exceeds(settings.input_limit_bytes):
        # Rejected before a single byte is fetched; a smaller file can follow.
        await message.answer(texts.ERROR_TOO_LARGE, reply_markup=back_to_menu())
        return

    await _run_voice_job(
        message=message,
        state=state,
        bot=bot,
        settings=settings,
        session=session,
        incoming=incoming,
        user_id=user_id,
        user=user,
        media_gate=media_gate,
    )


def _voice_service(settings: Settings) -> FfmpegVoiceNoteService:
    return FfmpegVoiceNoteService(
        ffmpeg_bin=settings.ffmpeg_bin,
        ffprobe_bin=settings.ffprobe_bin,
        timeout=settings.process_timeout_seconds,
        probe_timeout=min(settings.process_timeout_seconds, 120),
    )


def _is_heavy(media_gate: MediaJobGate | None, incoming: IncomingFile) -> bool:
    if media_gate is None:
        return False
    return media_gate.is_heavy(incoming.size) or (incoming.duration or 0) > _LONG_SOURCE_SECONDS


async def _run_voice_job(
    *,
    message: Message,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    session: AsyncSession,
    incoming: IncomingFile,
    user_id: int,
    user: User | None = None,
    media_gate: MediaJobGate | None = None,
) -> _Outcome:
    """Fetch, probe, encode and send - always cleaning up afterwards."""
    try:
        ticket = admit(
            media_gate,
            user_id,
            expected_bytes=estimate_job_bytes(incoming.size, extra=_VOICE_OUTPUT_RESERVE),
            heavy=_is_heavy(media_gate, incoming),
        )
    except ServerBusy as busy:
        # Nothing started; the state stays so the same file can be resent.
        await message.answer(busy_message(busy), reply_markup=back_to_menu())
        return _Outcome.BUSY

    job_id = uuid.uuid4()
    log_extra = {"job_id": str(job_id)}
    jobs = JobsRepository(session)
    job = None

    async def record_job():
        return await jobs.create(
            job_id=job_id,
            telegram_user_id=user_id,
            job_type=JobType.VOICE_NOTE,
            original_filename=incoming.original_filename,
            input_size=incoming.size,
        )

    status = await message.answer(texts.VOICE_PROCESSING)
    files = TelegramFileService.from_settings(bot, settings)
    service = _voice_service(settings)

    try:
        with ticket:
            async with job_workspace(settings.temp_root, job_id) as workspace:
                fetched = None
                try:
                    fetched = await files.fetch(incoming, workspace, job_id=str(job_id))
                    await service.probe(fetched.path, job_id=str(job_id))
                    job = await record_job()
                    await commit_early(session)

                    destination = workspace.new_file(".ogg", prefix="voice_")
                    result = await service.encode(fetched.path, destination, job_id=str(job_id))
                    ensure_sendable(result.size_bytes, settings.output_limit_bytes)
                    await _send_voice(bot, message, result)

                    await jobs.mark_success(job, output_size=result.size_bytes)
                    logger.info(
                        "voice job succeeded (%d bytes, %.1fs)",
                        result.size_bytes,
                        result.duration,
                        extra=log_extra,
                    )
                finally:
                    await files.release(fetched, job_id=str(job_id))
    except Exception as exc:  # noqa: BLE001 - mapped to friendly copy below
        if job is None:
            job = await record_job()
        await jobs.mark_failed(job, error_code=error_code(exc))
        if isinstance(exc, (MediaProcessingError, FileTooLargeError, OutputTooLargeError)):
            logger.warning("voice job failed: %s", error_code(exc), extra=log_extra)
        else:
            logger.exception("voice job failed", extra=log_extra)
        await message.answer(_failure_text(exc), reply_markup=back_to_menu())
        # Still listening, so the next file can follow straight away.
        await state.set_state(VoiceStates.waiting_for_media)
        return _Outcome.FAILED
    finally:
        await delete_quietly(status)

    await message.answer(texts.VOICE_DONE, reply_markup=voice_done())
    await state.clear()
    await maybe_send_cta(message, user)
    return _Outcome.DONE


async def _send_voice(bot: Bot, message: Message, result: VoiceNoteResult) -> Any:
    """``sendVoice`` - and proof that Telegram really stored a voice message."""
    method = message.answer_voice(
        FSInputFile(result.path, filename=VOICE_UPLOAD_NAME),
        duration=max(1, round(result.duration)),
    )
    try:
        sent = await send_media(bot, method, size_bytes=result.size_bytes)
    except TelegramEntityTooLarge as exc:
        raise MediaProcessingError(
            ProcessingErrorCode.OUTPUT_TOO_LARGE, "Bot API refused the upload size"
        ) from exc
    except TelegramAPIError as exc:
        if _VOICE_FORBIDDEN_MARKER in str(exc).upper():
            raise MediaProcessingError(
                ProcessingErrorCode.VOICE_FORBIDDEN, "recipient refuses voice messages"
            ) from exc
        raise MediaProcessingError(ProcessingErrorCode.SEND_FAILED, type(exc).__name__) from exc

    if getattr(sent, "voice", None) is None:
        # Delivered as something else (a file, a music track): take it back
        # rather than report a voice note that does not exist.
        await delete_quietly(sent)
        raise MediaProcessingError(
            ProcessingErrorCode.SEND_FAILED, "Telegram did not store a voice message"
        )
    return sent


def _failure_text(error: BaseException) -> str:
    if isinstance(error, MediaProcessingError) and error.code in _FAILURE_TEXTS:
        return _FAILURE_TEXTS[error.code]
    return user_message(error)


@router.callback_query(VoiceCallback.filter(F.action == "forward_help"))
async def show_forward_help(callback: CallbackQuery) -> None:
    await _replace(callback, texts.VOICE_FORWARD_HELP, voice_forward_help())


@router.callback_query(VoiceCallback.filter(F.action == "guide_ios"))
async def show_ios_guide(callback: CallbackQuery) -> None:
    await _replace(callback, texts.VOICE_FORWARD_HELP_IOS, voice_forward_help())


@router.callback_query(VoiceCallback.filter(F.action == "guide_android"))
async def show_android_guide(callback: CallbackQuery) -> None:
    await _replace(callback, texts.VOICE_FORWARD_HELP_ANDROID, voice_forward_help())


@router.callback_query(VoiceCallback.filter(F.action == "forward_back"))
async def back_to_result(callback: CallbackQuery) -> None:
    """Return to the success message the guide was opened from."""
    await _replace(callback, texts.VOICE_DONE, voice_done())


async def _replace(callback: CallbackQuery, text: str, markup) -> None:
    """Edit in place, falling back to a new message when Telegram refuses
    (an unchanged body, or a message too old to edit)."""
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(text, reply_markup=markup)
        except Exception:  # noqa: BLE001 - message may not be editable
            await callback.message.answer(text, reply_markup=markup)
    await callback.answer()
