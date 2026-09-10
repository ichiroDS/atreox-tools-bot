"""Video -> Telegram circle flow."""

from __future__ import annotations

import logging
import uuid

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.callbacks import CircleCallback, MenuCallback
from app.bot.errors import error_code, user_message
from app.bot.keyboards.circle import circle_done, forward_help
from app.bot.keyboards.common import back_to_menu, main_menu
from app.bot.promo import maybe_send_cta
from app.bot.states import CircleStates
from app.config import Settings
from app.db.models import Feature, JobType, User
from app.db.repositories import EventsRepository, JobsRepository
from app.services.media.circle import FfmpegVideoCircleService
from app.services.media.probe import MediaProbe
from app.services.ratelimit import RateLimiter
from app.services.telegram_files import TelegramFileService, extract_video
from app.utils.temp_files import job_workspace

logger = logging.getLogger(__name__)

router = Router(name="circle")


@router.callback_query(MenuCallback.filter(F.action == "circle"))
async def open_circle(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession | None = None,
) -> None:
    await state.set_state(CircleStates.waiting_for_video)
    if session is not None and callback.from_user is not None:
        await EventsRepository(session).record(callback.from_user.id, Feature.CIRCLE)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(texts.CIRCLE_PROMPT, reply_markup=back_to_menu())
    await callback.answer()


@router.message(CircleStates.waiting_for_video)
async def handle_video(
    message: Message,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    session: AsyncSession,
    limiter: RateLimiter | None = None,
    user: User | None = None,
) -> None:
    incoming = extract_video(message)
    if incoming is None:
        await message.answer(texts.CIRCLE_WRONG_INPUT, reply_markup=back_to_menu())
        return

    user_id = message.from_user.id if message.from_user else 0
    if limiter is not None and not limiter.allow("media", user_id):
        # The state is kept so the user can simply resend in a moment.
        await message.answer(texts.RATE_LIMITED, reply_markup=back_to_menu())
        return

    job_id = uuid.uuid4()
    log_extra = {"job_id": str(job_id)}
    jobs = JobsRepository(session)
    job = await jobs.create(
        job_id=job_id,
        telegram_user_id=message.from_user.id if message.from_user else 0,
        job_type=JobType.CIRCLE,
        original_filename=incoming.original_filename,
        input_size=incoming.size,
    )

    status = await message.answer(texts.CIRCLE_PROCESSING)
    files = TelegramFileService(bot, max_file_size_bytes=settings.max_file_size_bytes)
    service = FfmpegVideoCircleService(
        ffmpeg_bin=settings.ffmpeg_bin,
        probe=MediaProbe(settings.ffprobe_bin),
        size=settings.video_note_size,
        max_duration=settings.video_note_max_duration,
        timeout=settings.process_timeout_seconds,
    )

    try:
        async with job_workspace(settings.temp_root, job_id) as workspace:
            source = workspace.new_file(incoming.extension, prefix="src_")
            destination = workspace.new_file(".mp4", prefix="circle_")

            await files.download(incoming, source, job_id=str(job_id))
            result = await service.to_circle(source, destination, job_id=str(job_id))

            await message.answer_video_note(
                FSInputFile(result.path),
                length=settings.video_note_size,
            )
            await jobs.mark_success(job, output_size=result.size_bytes)
            logger.info("circle job succeeded", extra=log_extra)
    except Exception as exc:  # noqa: BLE001 - mapped to friendly copy below
        await jobs.mark_failed(job, error_code=error_code(exc))
        logger.exception("circle job failed", extra=log_extra)
        await message.answer(user_message(exc), reply_markup=main_menu())
        await state.clear()
        return
    finally:
        await _safe_delete(status)

    await message.answer(texts.CIRCLE_DONE, reply_markup=circle_done())
    await state.clear()
    await maybe_send_cta(message, user)


@router.callback_query(CircleCallback.filter(F.action == "forward_help"))
async def show_forward_help(callback: CallbackQuery) -> None:
    await _replace(callback, texts.CIRCLE_FORWARD_HELP, forward_help())


@router.callback_query(CircleCallback.filter(F.action == "guide_ios"))
async def show_ios_guide(callback: CallbackQuery) -> None:
    await _replace(callback, texts.CIRCLE_FORWARD_HELP_IOS, forward_help())


@router.callback_query(CircleCallback.filter(F.action == "guide_android"))
async def show_android_guide(callback: CallbackQuery) -> None:
    await _replace(callback, texts.CIRCLE_FORWARD_HELP_ANDROID, forward_help())


@router.callback_query(CircleCallback.filter(F.action == "forward_back"))
async def back_to_result(callback: CallbackQuery) -> None:
    """Return to the success message the guide was opened from."""
    await _replace(callback, texts.CIRCLE_DONE, circle_done())


async def _replace(callback: CallbackQuery, text: str, markup) -> None:
    """Edit in place, falling back to a new message when Telegram refuses
    (an unchanged body, or a message too old to edit)."""
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(text, reply_markup=markup)
        except Exception:  # noqa: BLE001 - message may not be editable
            await callback.message.answer(text, reply_markup=markup)
    await callback.answer()


async def _safe_delete(message: Message | None) -> None:
    if message is None:
        return
    try:
        await message.delete()
    except Exception:  # noqa: BLE001 - deletion is best effort
        pass
