"""Video -> Telegram circle flow.

A video that fits in one circle is converted straight away. A longer one gets
a choice - split it into consecutive circles, keep only the first, or cancel -
instead of being silently trimmed.
"""

from __future__ import annotations

import enum
import logging
import uuid

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.callbacks import CircleCallback, MenuCallback
from app.bot.errors import busy_message, error_code, user_message
from app.bot.keyboards.circle import circle_done, forward_help, long_video_choices
from app.bot.keyboards.common import back_to_menu, main_menu
from app.bot.media_jobs import admit, commit_early, send_media
from app.bot.promo import maybe_send_cta
from app.bot.states import CircleStates
from app.config import Settings
from app.db.models import Feature, JobType, User
from app.db.repositories import EventsRepository, JobsRepository
from app.services.jobgate import MediaJobGate, ServerBusy, estimate_job_bytes
from app.services.media.circle import FfmpegVideoCircleService, needs_split
from app.services.media.probe import MediaProbe
from app.services.ratelimit import RateLimiter
from app.services.telegram_files import (
    IncomingFile,
    TelegramFileService,
    ensure_sendable,
    extract_video,
)
from app.utils.temp_files import job_workspace

logger = logging.getLogger(__name__)

router = Router(name="circle")

# Disk a circle job needs on top of its source: one encoded segment at a time,
# each deleted once sent.
_CIRCLE_OUTPUT_RESERVE = 64 * 1024 * 1024


class CircleMode(str, enum.Enum):
    AUTO = "auto"    # one circle if it fits, otherwise ask
    FIRST = "first"  # the first circle only
    SPLIT = "split"  # the whole video, as consecutive circles


class _Outcome(str, enum.Enum):
    DONE = "done"
    ASKED = "asked"
    BUSY = "busy"
    FAILED = "failed"


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
    media_gate: MediaJobGate | None = None,
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

    if incoming.exceeds(settings.input_limit_bytes):
        # Rejected before a single byte is fetched; the state stays so a
        # smaller file can follow.
        await message.answer(texts.ERROR_TOO_LARGE, reply_markup=back_to_menu())
        return

    # Telegram declares the duration of native videos, so a long one can be
    # asked about before anything is downloaded.
    if needs_split(incoming.duration, settings.video_note_max_duration):
        await _ask_how_to_handle(message, state, incoming, incoming.duration or 0.0)
        return

    await _run_circle_job(
        message=message,
        state=state,
        bot=bot,
        settings=settings,
        session=session,
        incoming=incoming,
        mode=CircleMode.AUTO,
        user_id=user_id,
        user=user,
        media_gate=media_gate,
    )


@router.callback_query(
    CircleStates.choosing_long_mode, CircleCallback.filter(F.action.in_({"split", "first"}))
)
async def handle_long_video_choice(
    callback: CallbackQuery,
    callback_data: CircleCallback,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    session: AsyncSession,
    user: User | None = None,
    media_gate: MediaJobGate | None = None,
) -> None:
    data = await state.get_data()
    # Leave the choosing state before any network round trip, so a second tap
    # on the same keyboard cannot start a second job.
    await state.set_state(None)

    incoming = IncomingFile.from_dict(data.get("circle_file"))
    message = callback.message
    if incoming is None or not isinstance(message, Message):
        await state.clear()
        await callback.answer(texts.CIRCLE_CHOICE_EXPIRED, show_alert=True)
        return
    await callback.answer()

    mode = CircleMode.SPLIT if callback_data.action == "split" else CircleMode.FIRST
    outcome = await _run_circle_job(
        message=message,
        state=state,
        bot=bot,
        settings=settings,
        session=session,
        incoming=incoming,
        mode=mode,
        user_id=callback.from_user.id if callback.from_user else 0,
        user=user,
        media_gate=media_gate,
    )
    if outcome is _Outcome.BUSY:
        # Nothing started, so the same buttons can simply be tapped again.
        await state.set_state(CircleStates.choosing_long_mode)


@router.callback_query(CircleCallback.filter(F.action == "cancel"))
async def cancel_long_video(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(texts.MAIN_MENU, reply_markup=main_menu())
        except Exception:  # noqa: BLE001 - message may not be editable
            await callback.message.answer(texts.MAIN_MENU, reply_markup=main_menu())
    await callback.answer(texts.CANCELLED)


@router.callback_query(CircleCallback.filter(F.action.in_({"split", "first"})))
async def stale_long_video_choice(callback: CallbackQuery) -> None:
    """A tap on an old keyboard: the job already ran, or the bot restarted."""
    await callback.answer(texts.CIRCLE_CHOICE_EXPIRED, show_alert=True)


async def _ask_how_to_handle(
    message: Message, state: FSMContext, incoming: IncomingFile, duration: float
) -> None:
    await state.set_state(CircleStates.choosing_long_mode)
    await state.update_data(circle_file=incoming.to_dict(), circle_duration=duration)
    await message.answer(texts.circle_long_video(duration), reply_markup=long_video_choices())


def _circle_service(settings: Settings) -> FfmpegVideoCircleService:
    return FfmpegVideoCircleService(
        ffmpeg_bin=settings.ffmpeg_bin,
        probe=MediaProbe(settings.ffprobe_bin, timeout=min(settings.process_timeout_seconds, 120)),
        size=settings.video_note_size,
        max_duration=settings.video_note_max_duration,
        timeout=settings.process_timeout_seconds,
    )


async def _run_circle_job(
    *,
    message: Message,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    session: AsyncSession,
    incoming: IncomingFile,
    mode: CircleMode,
    user_id: int,
    user: User | None = None,
    media_gate: MediaJobGate | None = None,
) -> _Outcome:
    """Fetch, probe, encode and send - always cleaning up afterwards."""
    heavy = mode is CircleMode.SPLIT or (
        media_gate is not None and media_gate.is_heavy(incoming.size)
    )
    try:
        ticket = admit(
            media_gate,
            user_id,
            expected_bytes=estimate_job_bytes(incoming.size, extra=_CIRCLE_OUTPUT_RESERVE),
            heavy=heavy,
        )
    except ServerBusy as busy:
        await message.answer(busy_message(busy), reply_markup=back_to_menu())
        return _Outcome.BUSY

    job_id = uuid.uuid4()
    log_extra = {"job_id": str(job_id)}
    jobs = JobsRepository(session)
    job = None
    sent = total = output_bytes = 0

    async def record_job():
        return await jobs.create(
            job_id=job_id,
            telegram_user_id=user_id,
            job_type=JobType.CIRCLE,
            original_filename=incoming.original_filename,
            input_size=incoming.size,
        )

    status = await message.answer(texts.CIRCLE_PROCESSING)
    files = TelegramFileService.from_settings(bot, settings)
    service = _circle_service(settings)

    try:
        with ticket:
            async with job_workspace(settings.temp_root, job_id) as workspace:
                fetched = None
                keep_server_copy = False
                try:
                    fetched = await files.fetch(incoming, workspace, job_id=str(job_id))
                    info = await service.probe(fetched.path, job_id=str(job_id))
                    segments = service.plan(info)

                    if mode is CircleMode.AUTO and len(segments) > 1:
                        # Only a file (not a native video) gets here: its length
                        # was unknown until probed. The user is asked, and the
                        # server keeps its copy for the answer.
                        keep_server_copy = True
                        await _ask_how_to_handle(message, state, incoming, info.duration)
                        return _Outcome.ASKED
                    if mode is not CircleMode.SPLIT:
                        segments = segments[:1]

                    total = len(segments)
                    job = await record_job()
                    await commit_early(session)

                    for segment in segments:
                        if total > 1:
                            await _set_status(status, texts.circle_progress(segment.index + 1, total))
                        destination = workspace.new_file(
                            ".mp4", prefix=f"circle{segment.index + 1:04d}_"
                        )
                        result = await service.encode_segment(
                            fetched.path,
                            destination,
                            segment,
                            with_audio=info.has_audio,
                            job_id=str(job_id),
                        )
                        ensure_sendable(result.size_bytes, settings.output_limit_bytes)
                        await send_media(
                            bot,
                            message.answer_video_note(
                                FSInputFile(result.path),
                                length=settings.video_note_size,
                                duration=max(1, round(segment.duration)),
                            ),
                            size_bytes=result.size_bytes,
                        )
                        sent += 1
                        output_bytes += result.size_bytes
                        # One segment on disk at a time.
                        destination.unlink(missing_ok=True)

                    await jobs.mark_success(job, output_size=output_bytes)
                    logger.info(
                        "circle job succeeded (%d circle(s), %d bytes)",
                        sent,
                        output_bytes,
                        extra=log_extra,
                    )
                finally:
                    if not keep_server_copy:
                        await files.release(fetched, job_id=str(job_id))
    except Exception as exc:  # noqa: BLE001 - mapped to friendly copy below
        if job is None:
            job = await record_job()
        await jobs.mark_failed(job, error_code=error_code(exc))
        logger.exception("circle job failed after %d/%d circle(s)", sent, total, extra=log_extra)
        text = texts.circle_partial_failure(sent, total) if sent else user_message(exc)
        await message.answer(text, reply_markup=main_menu())
        await state.clear()
        return _Outcome.FAILED
    finally:
        await _safe_delete(status)

    await message.answer(texts.CIRCLE_DONE, reply_markup=circle_done())
    await state.clear()
    await maybe_send_cta(message, user)
    return _Outcome.DONE


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


async def _set_status(status: Message | None, text: str) -> None:
    """Progress is cosmetic: a refused edit must never fail the job."""
    if status is None:
        return
    try:
        await status.edit_text(text)
    except Exception:  # noqa: BLE001 - editing is best effort
        pass


async def _safe_delete(message: Message | None) -> None:
    if message is None:
        return
    try:
        await message.delete()
    except Exception:  # noqa: BLE001 - deletion is best effort
        pass
