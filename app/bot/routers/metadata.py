"""Metadata Studio: clean metadata, or rewrite device / location / time."""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.callbacks import MenuCallback, MetadataCallback
from app.bot.errors import error_code, user_message
from app.bot.keyboards.common import back_to_menu, main_menu
from app.bot.promo import maybe_send_cta
from app.bot.keyboards.metadata import (
    change_done_choices,
    city_choices,
    clean_done_choices,
    confirmation_choices,
    country_choices,
    generation_choices,
    metadata_menu,
    model_choices,
    time_of_day_choices,
)
from app.bot.states import ChangeMetadataStates, CleanMetadataStates
from app.config import Settings
from app.data.device_presets import (
    generation_label,
    get_preset,
    presets_for_generation,
)
from app.data.locations import get_city, get_country
from app.db.models import Feature, JobType, User
from app.db.repositories import EventsRepository, JobsRepository
from app.services.media.base import MediaProcessingError, ProcessingErrorCode
from app.services.media.metadata import (
    LocationFix,
    MetadataChangeRequest,
    MetadataService,
)
from app.services.ratelimit import RateLimiter
from app.services.telegram_files import (
    FileKind,
    IncomingFile,
    TelegramFileService,
    extract_media,
)
from app.services.timeofday import TimeOfDay, pick_datetime
from app.utils.temp_files import JobWorkspace, job_workspace

logger = logging.getLogger(__name__)

router = Router(name="metadata")


# --- Menu -------------------------------------------------------------------


@router.callback_query(MenuCallback.filter(F.action == "metadata"))
async def open_metadata(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession | None = None,
) -> None:
    await state.clear()
    if session is not None and callback.from_user is not None:
        await EventsRepository(session).record(callback.from_user.id, Feature.METADATA)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(texts.METADATA_MENU, reply_markup=metadata_menu())
    await callback.answer()


@router.callback_query(MetadataCallback.filter(F.action == "cancel"))
async def cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(texts.MAIN_MENU, reply_markup=main_menu())
    await callback.answer(texts.CANCELLED)


# --- Clean ------------------------------------------------------------------


@router.callback_query(MetadataCallback.filter(F.action == "clean"))
async def start_clean(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(CleanMetadataStates.waiting_for_file)
    await _replace(callback, texts.METADATA_CLEAN_PROMPT, back_to_menu())


@router.message(CleanMetadataStates.waiting_for_file)
async def handle_clean_file(
    message: Message,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    session: AsyncSession,
    limiter: RateLimiter | None = None,
    user: User | None = None,
) -> None:
    incoming = extract_media(message)
    if incoming is None:
        await message.answer(texts.METADATA_WRONG_INPUT, reply_markup=back_to_menu())
        return
    if not _within_rate_limit(limiter, message):
        await message.answer(texts.RATE_LIMITED, reply_markup=back_to_menu())
        return
    if incoming.compressed:
        await message.answer(texts.METADATA_COMPRESSED_WARNING)

    service = MetadataService(
        exiftool_bin=settings.exiftool_path, timeout=settings.process_timeout_seconds
    )

    async def operation(workspace: JobWorkspace, working_copy: Path):
        return await service.clean(working_copy, job_id=str(workspace.job_id))

    await _run_metadata_job(
        message=message,
        state=state,
        bot=bot,
        settings=settings,
        session=session,
        incoming=incoming,
        job_type=JobType.METADATA_CLEAN,
        prefix="atreox",
        done_text=texts.METADATA_CLEAN_DONE,
        done_markup=clean_done_choices(),
        operation=operation,
        user=user,
    )


# --- Change wizard ----------------------------------------------------------


@router.callback_query(MetadataCallback.filter(F.action == "change"))
async def start_change(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ChangeMetadataStates.waiting_for_file)
    await _replace(callback, texts.METADATA_CHANGE_PROMPT, back_to_menu())


@router.message(ChangeMetadataStates.waiting_for_file)
async def handle_change_file(message: Message, state: FSMContext) -> None:
    incoming = extract_media(message)
    if incoming is None:
        await message.answer(texts.METADATA_WRONG_INPUT, reply_markup=back_to_menu())
        return
    if incoming.compressed:
        await message.answer(texts.METADATA_COMPRESSED_WARNING)

    await state.update_data(file=_serialise(incoming))
    await state.set_state(ChangeMetadataStates.choosing_generation)
    await message.answer(
        texts.DEVICE_GENERATION_PROMPT, reply_markup=generation_choices()
    )


# --- step 1: device (generation, then variant) ------------------------------


@router.callback_query(
    ChangeMetadataStates.choosing_generation, MetadataCallback.filter(F.action == "gen")
)
async def handle_generation(
    callback: CallbackQuery, callback_data: MetadataCallback, state: FSMContext
) -> None:
    generation = _as_int(callback_data.value)
    if not presets_for_generation(generation):
        await callback.answer(texts.ERROR_GENERIC, show_alert=True)
        return

    await state.update_data(generation=generation)
    await state.set_state(ChangeMetadataStates.choosing_model)
    await _replace(
        callback,
        texts.device_model_prompt(generation_label(generation)),
        model_choices(generation),
    )


@router.callback_query(
    ChangeMetadataStates.choosing_model, MetadataCallback.filter(F.action == "device")
)
async def handle_device(
    callback: CallbackQuery, callback_data: MetadataCallback, state: FSMContext
) -> None:
    preset = get_preset(callback_data.value)
    if preset is None:
        await callback.answer(texts.ERROR_GENERIC, show_alert=True)
        return

    await state.update_data(device_key=preset.key)
    if await _has_every_answer(state):
        await _show_confirmation(callback, state)
        return

    await state.set_state(ChangeMetadataStates.choosing_country)
    await _replace(callback, texts.COUNTRY_PROMPT, country_choices())


# --- step 2: location (country, then city) ----------------------------------


@router.callback_query(
    ChangeMetadataStates.choosing_country, MetadataCallback.filter(F.action == "country")
)
async def handle_country(
    callback: CallbackQuery, callback_data: MetadataCallback, state: FSMContext
) -> None:
    country = get_country(callback_data.value)
    if country is None:
        await callback.answer(texts.ERROR_GENERIC, show_alert=True)
        return

    await state.update_data(country_key=country.key)
    await state.set_state(ChangeMetadataStates.choosing_city)
    await _replace(callback, texts.city_prompt(country.label), city_choices(country.key))


@router.callback_query(
    ChangeMetadataStates.choosing_city, MetadataCallback.filter(F.action == "city")
)
async def handle_city(
    callback: CallbackQuery, callback_data: MetadataCallback, state: FSMContext
) -> None:
    found = get_city(callback_data.value)
    if found is None:
        await callback.answer(texts.ERROR_GENERIC, show_alert=True)
        return

    await state.update_data(city_key=found[1].key)
    if await _has_every_answer(state):
        await _show_confirmation(callback, state)
        return

    await state.set_state(ChangeMetadataStates.choosing_time_of_day)
    await _replace(callback, texts.TIME_OF_DAY_PROMPT, time_of_day_choices())


# --- step 3: time of day ----------------------------------------------------


@router.callback_query(
    ChangeMetadataStates.choosing_time_of_day, MetadataCallback.filter(F.action == "tod")
)
async def handle_time_of_day(
    callback: CallbackQuery, callback_data: MetadataCallback, state: FSMContext
) -> None:
    try:
        time_of_day = TimeOfDay(callback_data.value)
    except ValueError:
        await callback.answer(texts.ERROR_GENERIC, show_alert=True)
        return

    await state.update_data(time_of_day=time_of_day.value)
    await _show_confirmation(callback, state)


# --- confirmation -----------------------------------------------------------


def _as_int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


async def _has_every_answer(state: FSMContext) -> bool:
    """True once device, city and time of day have all been chosen."""
    data = await state.get_data()
    return bool(
        data.get("device_key") and data.get("city_key") and data.get("time_of_day")
    )


async def _show_confirmation(callback: CallbackQuery, state: FSMContext) -> None:
    """Render the summary, or bail out if an answer went missing."""
    await state.set_state(ChangeMetadataStates.confirming)
    data = await state.get_data()
    preset = get_preset(data.get("device_key", ""))
    found = get_city(data.get("city_key", ""))
    if preset is None or found is None or not data.get("time_of_day"):
        await callback.answer(texts.ERROR_GENERIC, show_alert=True)
        return

    country, city = found
    summary = texts.confirmation(
        device=preset.label,
        city=city.label,
        country=country.label,
        time_of_day=str(data["time_of_day"]),
    )
    await _replace(callback, summary, confirmation_choices())


# One answer at a time: the others survive, so "Change Location" does not make
# the user re-pick their device.
_EDIT_STEPS = {
    "device": ChangeMetadataStates.choosing_generation,
    "location": ChangeMetadataStates.choosing_country,
    "time": ChangeMetadataStates.choosing_time_of_day,
}


# Not state-filtered: the same button is also the "Back" control inside the
# second half of the device and location steps.
@router.callback_query(MetadataCallback.filter(F.action == "edit"))
async def edit_step(
    callback: CallbackQuery, callback_data: MetadataCallback, state: FSMContext
) -> None:
    """Jump back to the start of a single wizard step."""
    step = _EDIT_STEPS.get(callback_data.value)
    if step is None:
        await callback.answer(texts.ERROR_GENERIC, show_alert=True)
        return

    await state.set_state(step)
    if step is ChangeMetadataStates.choosing_generation:
        await _replace(callback, texts.DEVICE_GENERATION_PROMPT, generation_choices())
    elif step is ChangeMetadataStates.choosing_country:
        await _replace(callback, texts.COUNTRY_PROMPT, country_choices())
    else:
        await _replace(callback, texts.TIME_OF_DAY_PROMPT, time_of_day_choices())


@router.callback_query(
    ChangeMetadataStates.confirming, MetadataCallback.filter(F.action == "apply")
)
async def apply_changes(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    session: AsyncSession,
    limiter: RateLimiter | None = None,
    user: User | None = None,
) -> None:
    await callback.answer()
    data = await state.get_data()
    message = callback.message
    if not isinstance(message, Message):
        return

    incoming = _deserialise(data.get("file"))
    preset = get_preset(data.get("device_key", ""))
    found = get_city(data.get("city_key", ""))
    if incoming is None or preset is None or found is None or not data.get("time_of_day"):
        await state.clear()
        await message.answer(texts.ERROR_GENERIC, reply_markup=main_menu())
        return

    if not _within_rate_limit(limiter, callback):
        # The confirmation screen stays put, so one tap retries.
        await message.answer(texts.RATE_LIMITED, reply_markup=confirmation_choices())
        return

    _, city = found
    time_of_day = TimeOfDay(data["time_of_day"])

    # The chosen city fixes the timezone, so the capture time lands in that
    # city's local evening rather than the server's.
    tz = timezone(timedelta(hours=city.utc_offset))
    taken_at = pick_datetime(time_of_day, on_date=datetime.now(tz).date(), tz=tz)
    latitude, longitude = city.jittered()
    request = MetadataChangeRequest(
        preset=preset,
        taken_at=taken_at,
        location=LocationFix(latitude=latitude, longitude=longitude),
        time_of_day=time_of_day,
        altitude=city.altitude,
    )

    service = MetadataService(
        exiftool_bin=settings.exiftool_path, timeout=settings.process_timeout_seconds
    )

    async def operation(workspace: JobWorkspace, working_copy: Path):
        return await service.change(working_copy, request, job_id=str(workspace.job_id))

    await _run_metadata_job(
        message=message,
        state=state,
        bot=bot,
        settings=settings,
        session=session,
        incoming=incoming,
        job_type=JobType.METADATA_CHANGE,
        prefix="atreox",
        done_text=texts.METADATA_CHANGE_DONE,
        done_markup=change_done_choices(),
        operation=operation,
        acting_user_id=callback.from_user.id if callback.from_user else 0,
        user=user,
    )


# --- Shared job runner ------------------------------------------------------


async def _run_metadata_job(
    *,
    message: Message,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    session: AsyncSession,
    incoming: IncomingFile,
    job_type: JobType,
    prefix: str,
    done_text: str,
    done_markup,
    operation,
    acting_user_id: int | None = None,
    user: User | None = None,
) -> None:
    """Download, copy, process and send back - always cleaning up afterwards."""
    user_id = acting_user_id
    if user_id is None:
        user_id = message.from_user.id if message.from_user else 0

    job_id = uuid.uuid4()
    log_extra = {"job_id": str(job_id)}
    jobs = JobsRepository(session)
    job = await jobs.create(
        job_id=job_id,
        telegram_user_id=user_id,
        job_type=job_type,
        original_filename=incoming.original_filename,
        input_size=incoming.size,
    )

    status = await message.answer(texts.METADATA_PROCESSING)
    files = TelegramFileService(bot, max_file_size_bytes=settings.max_file_size_bytes)

    try:
        async with job_workspace(settings.temp_root, job_id) as workspace:
            source = workspace.new_file(incoming.extension, prefix="src_")
            await files.download(incoming, source, job_id=str(job_id))

            # Always operate on a copy so the original stays untouched.
            working_copy = workspace.new_file(incoming.extension, prefix=f"{prefix}_")
            await asyncio.to_thread(shutil.copy2, source, working_copy)

            result = await operation(workspace, working_copy)
            if result.size_bytes > settings.max_file_size_bytes:
                raise MediaProcessingError(ProcessingErrorCode.TOO_LARGE, "output too large")

            await message.answer_document(
                FSInputFile(
                    result.path, filename=incoming.output_filename(prefix=prefix)
                )
            )
            await jobs.mark_success(job, output_size=result.size_bytes)
            logger.info("%s job succeeded", job_type.value, extra=log_extra)
    except Exception as exc:  # noqa: BLE001 - mapped to friendly copy below
        await jobs.mark_failed(job, error_code=error_code(exc))
        logger.exception("%s job failed", job_type.value, extra=log_extra)
        await message.answer(user_message(exc), reply_markup=main_menu())
        await state.clear()
        return
    finally:
        try:
            await status.delete()
        except Exception:  # noqa: BLE001 - deletion is best effort
            pass

    await message.answer(done_text, reply_markup=done_markup)
    await state.clear()
    await maybe_send_cta(message, user)


def _within_rate_limit(limiter: RateLimiter | None, event) -> bool:
    """Fair-use check for the media paths, keyed by the acting user."""
    if limiter is None:
        return True
    user = getattr(event, "from_user", None)
    return limiter.allow("media", user.id if user else 0)


async def _replace(callback: CallbackQuery, text: str, markup) -> None:
    """Edit the wizard message in place, falling back to a new one."""
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(text, reply_markup=markup)
        except Exception:  # noqa: BLE001 - message may not be editable
            await callback.message.answer(text, reply_markup=markup)
    await callback.answer()


def _serialise(incoming: IncomingFile) -> dict:
    return {
        "file_id": incoming.file_id,
        "kind": incoming.kind.value,
        "size": incoming.size,
        "original_filename": incoming.original_filename,
        "mime_type": incoming.mime_type,
        "compressed": incoming.compressed,
    }


def _deserialise(payload: dict | None) -> IncomingFile | None:
    if not payload:
        return None
    try:
        return IncomingFile(
            file_id=payload["file_id"],
            kind=FileKind(payload["kind"]),
            size=payload.get("size"),
            original_filename=payload.get("original_filename"),
            mime_type=payload.get("mime_type"),
            compressed=bool(payload.get("compressed", False)),
        )
    except (KeyError, ValueError):
        return None
