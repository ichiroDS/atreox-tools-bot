"""Make Sticker flow: an image in, a real sticker back.

Static stickers only: the image is fitted to Telegram's 512 px box, given the
outline the user picked, and sent with ``sendSticker`` - and the reply is
checked, because a sticker that arrives as a file is not what was asked for.
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
from app.bot.callbacks import MakeStickerCallback, MenuCallback
from app.bot.errors import busy_message, error_code, user_message
from app.bot.keyboards.common import back_to_menu, main_menu
from app.bot.keyboards.make_sticker import sticker_done, style_choices
from app.bot.media_jobs import admit, commit_early, delete_quietly, send_media
from app.bot.promo import maybe_send_cta
from app.bot.states import MakeStickerStates
from app.config import Settings
from app.db.models import Feature, JobType, User
from app.db.repositories import EventsRepository, JobsRepository
from app.services.jobgate import MediaJobGate, ServerBusy, estimate_job_bytes
from app.services.media.base import MediaProcessingError, ProcessingErrorCode
from app.services.media.sticker import (
    STICKER_FILENAME,
    StickerImage,
    StickerService,
    StickerStyle,
)
from app.services.ratelimit import RateLimiter
from app.services.telegram_files import (
    FileTooLargeError,
    IncomingFile,
    OutputTooLargeError,
    TelegramFileService,
    ensure_sendable,
    extract_image_source,
)
from app.utils.temp_files import job_workspace

logger = logging.getLogger(__name__)

router = Router(name="make_sticker")

# A sticker is half a megabyte at most; the source picture is the whole cost.
_DISK_FACTOR = 1.5
_SCRATCH_RESERVE = 8 * 1024 * 1024

_FAILURE_TEXTS: dict[ProcessingErrorCode, str] = {
    ProcessingErrorCode.UNSUPPORTED: texts.STICKER_MAKE_UNSUPPORTED,
    ProcessingErrorCode.TOO_LARGE: texts.STICKER_MAKE_UNSUPPORTED,
    ProcessingErrorCode.TOO_SMALL: texts.STICKER_MAKE_TOO_SMALL,
    ProcessingErrorCode.PROBE_FAILED: texts.STICKER_MAKE_CORRUPT,
    ProcessingErrorCode.CORRUPT_INPUT: texts.STICKER_MAKE_CORRUPT,
    ProcessingErrorCode.EMPTY_OUTPUT: texts.STICKER_MAKE_CORRUPT,
    ProcessingErrorCode.TIMEOUT: texts.OPTIMIZE_TIMEOUT,
    ProcessingErrorCode.VERIFY_FAILED: texts.STICKER_MAKE_SEND_FAILED,
    ProcessingErrorCode.SEND_FAILED: texts.STICKER_MAKE_SEND_FAILED,
    ProcessingErrorCode.DISK_FULL: texts.OPTIMIZE_DISK_FULL,
    ProcessingErrorCode.PROCESS_KILLED: texts.OPTIMIZE_OUT_OF_MEMORY,
}


class _Outcome(str, enum.Enum):
    DONE = "done"
    BUSY = "busy"
    FAILED = "failed"


@router.callback_query(MenuCallback.filter(F.action == "make_sticker"))
async def open_make_sticker(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession | None = None
) -> None:
    await state.clear()
    await state.set_state(MakeStickerStates.waiting_for_image)
    if session is not None and callback.from_user is not None:
        await EventsRepository(session).record(callback.from_user.id, Feature.MAKE_STICKER)
    await _replace(callback, texts.STICKER_MAKE_PROMPT, back_to_menu())


# A new image is welcome while the styles are on screen, and replaces the old.
@router.message(
    StateFilter(MakeStickerStates.waiting_for_image, MakeStickerStates.choosing_style)
)
async def handle_image(
    message: Message,
    state: FSMContext,
    settings: Settings,
    limiter: RateLimiter | None = None,
) -> None:
    incoming = extract_image_source(message)
    if incoming is None:
        await message.answer(texts.STICKER_MAKE_UNSUPPORTED, reply_markup=back_to_menu())
        return

    user_id = message.from_user.id if message.from_user else 0
    if limiter is not None and not limiter.allow("media", user_id):
        await message.answer(texts.RATE_LIMITED, reply_markup=back_to_menu())
        return
    if incoming.exceeds(settings.input_limit_bytes):
        await message.answer(texts.ERROR_TOO_LARGE, reply_markup=back_to_menu())
        return

    # Nothing is fetched yet: the style is chosen against the file reference.
    await state.set_data({"file": incoming.to_dict()})
    await state.set_state(MakeStickerStates.choosing_style)
    await message.answer(texts.STICKER_MAKE_STYLE_PROMPT, reply_markup=style_choices())


@router.callback_query(
    MakeStickerStates.choosing_style, MakeStickerCallback.filter(F.action == "style")
)
async def handle_style(
    callback: CallbackQuery,
    callback_data: MakeStickerCallback,
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
    try:
        style = StickerStyle(callback_data.value)
    except ValueError:
        style = None
    if incoming is None or style is None or not isinstance(message, Message):
        await state.clear()
        await callback.answer(texts.STICKER_MAKE_CHOICE_EXPIRED, show_alert=True)
        return
    await callback.answer()

    # Leave the choosing state so a second tap cannot start a second job.
    await state.set_state(None)
    outcome = await _run_job(
        message=message, state=state, bot=bot, settings=settings, session=session,
        incoming=incoming, style=style, user_id=_user_id(callback), user=user,
        media_gate=media_gate,
    )
    if outcome is not _Outcome.DONE:
        # The styles are still on screen: another one can simply be tapped.
        await state.set_state(MakeStickerStates.choosing_style)


@router.callback_query(MakeStickerCallback.filter(F.action == "cancel"))
async def cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _replace(callback, texts.MAIN_MENU, main_menu(), notice=texts.CANCELLED)


@router.callback_query(MakeStickerCallback.filter(F.action == "style"))
async def stale_style(callback: CallbackQuery) -> None:
    """A tap on an old keyboard: the job already ran, or the bot restarted."""
    await callback.answer(texts.STICKER_MAKE_CHOICE_EXPIRED, show_alert=True)


def _sticker_service(settings: Settings) -> StickerService:
    return StickerService(
        ffprobe_bin=settings.ffprobe_bin,
        timeout=settings.process_timeout_seconds,
        probe_timeout=min(settings.process_timeout_seconds, 120),
    )


async def _run_job(
    *,
    message: Message,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    session: AsyncSession,
    incoming: IncomingFile,
    style: StickerStyle,
    user_id: int,
    user: User | None,
    media_gate: MediaJobGate | None,
) -> _Outcome:
    """Fetch, convert, verify and send as a sticker - always cleaning up after."""
    try:
        ticket = admit(
            media_gate, user_id,
            expected_bytes=estimate_job_bytes(
                incoming.size, factor=_DISK_FACTOR, extra=_SCRATCH_RESERVE
            ),
            heavy=media_gate is not None and media_gate.is_heavy(incoming.size),
        )
    except ServerBusy as busy:
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
            job_type=JobType.MAKE_STICKER,
            original_filename=incoming.original_filename,
            input_size=incoming.size,
        )

    status = await message.answer(texts.STICKER_MAKE_PROCESSING)
    files = TelegramFileService.from_settings(bot, settings)
    service = _sticker_service(settings)

    try:
        with ticket:
            async with job_workspace(settings.temp_root, job_id) as workspace:
                fetched = None
                try:
                    fetched = await files.fetch(incoming, workspace, job_id=str(job_id))
                    analysis = await service.analyze(fetched.path, job_id=str(job_id))
                    job = await record_job()
                    await commit_early(session)

                    result = await service.make(
                        fetched.path, workspace, analysis, style, job_id=str(job_id)
                    )
                    ensure_sendable(result.size_bytes, settings.output_limit_bytes)
                    await _send_sticker(bot, message, result)

                    await jobs.mark_success(job, output_size=result.size_bytes)
                    logger.info(
                        "sticker job succeeded (%s, %d bytes)", style.value, result.size_bytes,
                        extra=log_extra,
                    )
                finally:
                    await files.release(fetched, job_id=str(job_id))
    except Exception as exc:  # noqa: BLE001 - mapped to friendly copy below
        if job is None:
            job = await record_job()
        await jobs.mark_failed(job, error_code=error_code(exc))
        if isinstance(exc, (MediaProcessingError, FileTooLargeError, OutputTooLargeError)):
            logger.warning("sticker job failed: %s", error_code(exc), extra=log_extra)
        else:
            logger.exception("sticker job failed", extra=log_extra)
        await message.answer(_failure_text(exc), reply_markup=back_to_menu())
        return _Outcome.FAILED
    finally:
        await delete_quietly(status)

    await state.clear()
    await message.answer(texts.STICKER_MAKE_DONE, reply_markup=sticker_done())
    await maybe_send_cta(message, user)
    return _Outcome.DONE


async def _send_sticker(bot: Bot, message: Message, result: StickerImage) -> Any:
    """``sendSticker`` - and proof that Telegram really stored a sticker."""
    method = message.answer_sticker(FSInputFile(result.path, filename=STICKER_FILENAME))
    try:
        sent = await send_media(bot, method, size_bytes=result.size_bytes)
    except TelegramEntityTooLarge as exc:
        raise MediaProcessingError(
            ProcessingErrorCode.OUTPUT_TOO_LARGE, "the upload was refused as too large"
        ) from exc
    except TelegramAPIError as exc:
        raise MediaProcessingError(ProcessingErrorCode.SEND_FAILED, type(exc).__name__) from exc

    if getattr(sent, "sticker", None) is None:
        # Delivered as something else: take it back rather than claim a sticker
        # that does not exist.
        await delete_quietly(sent)
        raise MediaProcessingError(
            ProcessingErrorCode.SEND_FAILED, "Telegram did not store a sticker"
        )
    return sent


def _failure_text(error: BaseException) -> str:
    if isinstance(error, MediaProcessingError) and error.code in _FAILURE_TEXTS:
        return _FAILURE_TEXTS[error.code]
    return user_message(error)


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
