"""Watermark flow: a file, a line of text, four quick choices, done.

The file is only referenced while the wizard runs - nothing is fetched until
Apply, so walking back and forth through the steps costs no transfers and no
disk. Saved presets carry the text *and* the look, so a returning user is two
taps from the same watermark they used last time.
"""

from __future__ import annotations

import base64
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
from app.bot.callbacks import MenuCallback, WatermarkCallback
from app.bot.errors import busy_message, error_code, user_message
from app.bot.keyboards.common import back_to_menu, main_menu
from app.bot.keyboards.watermark import (
    confirmation_choices,
    type_choices,
    delete_confirmation,
    opacity_choices,
    position_choices,
    preset_detail,
    preset_list,
    size_choices,
    style_choices,
    text_source_choices,
    watermark_done,
)
from app.bot.media_jobs import (
    ProgressReporter,
    admit,
    commit_early,
    delete_quietly,
    send_media,
)
from app.bot.promo import maybe_send_cta
from app.bot.states import WatermarkStates
from app.config import Settings
from app.db.models import (
    MAX_LOGO_BYTES,
    MAX_PRESETS_PER_USER,
    Feature,
    JobType,
    User,
    WatermarkPreset,
)
from app.db.repositories import (
    DuplicateName,
    EventsRepository,
    JobsRepository,
    PresetLimitReached,
    WatermarkPresetsRepository,
)
from app.services.jobgate import MediaJobGate, ServerBusy, estimate_job_bytes
from app.services.media.base import MediaProcessingError, ProcessingErrorCode
from app.services.media.optimizer import VideoAnalysis
from app.services.media.watermark import (
    LOGO_FILENAME,
    OPACITIES,
    LogoSpec,
    Position,
    Size,
    Style,
    WatermarkService,
    WatermarkSpec,
    WatermarkedMedia,
    clean_watermark_text,
    validate_logo,
    watermarked_filename,
)
from app.services.ratelimit import RateLimiter
from app.services.telegram_files import (
    FileKind,
    FileTooLargeError,
    IncomingFile,
    OutputTooLargeError,
    TelegramFileService,
    ensure_sendable,
    extract_image_source,
    extract_optimizer_source,
)
from app.services.media.optimizer import IMAGE_EXTENSIONS
from app.utils.temp_files import job_workspace

logger = logging.getLogger(__name__)

router = Router(name="watermark")

# The source plus one re-encoded copy.
_DISK_FACTOR = 2.0
_SCRATCH_RESERVE = 16 * 1024 * 1024
_LONG_VIDEO_SECONDS = 120

# Steps that are part of the wizard proper: a new file is welcome in any of
# them, and restarts the flow.
_WIZARD_STATES = (
    WatermarkStates.waiting_for_media,
    WatermarkStates.choosing_type,
    WatermarkStates.choosing_source,
    WatermarkStates.choosing_position,
    WatermarkStates.choosing_style,
    WatermarkStates.choosing_size,
    WatermarkStates.choosing_opacity,
    WatermarkStates.confirming,
)

_FAILURE_TEXTS: dict[ProcessingErrorCode, str] = {
    ProcessingErrorCode.UNSUPPORTED: texts.WATERMARK_UNSUPPORTED,
    ProcessingErrorCode.PROBE_FAILED: texts.WATERMARK_CORRUPT,
    ProcessingErrorCode.CORRUPT_INPUT: texts.WATERMARK_CORRUPT,
    ProcessingErrorCode.EMPTY_OUTPUT: texts.WATERMARK_CORRUPT,
    ProcessingErrorCode.TIMEOUT: texts.WATERMARK_TIMEOUT,
    ProcessingErrorCode.VERIFY_FAILED: texts.WATERMARK_VERIFY_FAILED,
    ProcessingErrorCode.TOOL_MISSING: texts.WATERMARK_FONT_MISSING,
    ProcessingErrorCode.TOO_LARGE: texts.WATERMARK_LOGO_TOO_LARGE,
    ProcessingErrorCode.DISK_FULL: texts.OPTIMIZE_DISK_FULL,
    ProcessingErrorCode.PROCESS_KILLED: texts.OPTIMIZE_OUT_OF_MEMORY,
    ProcessingErrorCode.SEND_FAILED: texts.OPTIMIZE_SEND_FAILED,
}


class _Outcome(str, enum.Enum):
    DONE = "done"
    BUSY = "busy"
    FAILED = "failed"


# --- entry and the file ------------------------------------------------------


@router.callback_query(MenuCallback.filter(F.action == "watermark"))
async def open_watermark(
    callback: CallbackQuery,
    state: FSMContext,
    session: AsyncSession | None = None,
) -> None:
    await state.clear()
    await state.set_state(WatermarkStates.waiting_for_media)
    if session is not None and callback.from_user is not None:
        await EventsRepository(session).record(callback.from_user.id, Feature.WATERMARK)
    await _replace(callback, texts.WATERMARK_PROMPT, back_to_menu())


@router.message(StateFilter(*_WIZARD_STATES))
async def handle_media(
    message: Message,
    state: FSMContext,
    settings: Settings,
    limiter: RateLimiter | None = None,
) -> None:
    incoming = extract_optimizer_source(message)
    if incoming is None:
        await message.answer(texts.WATERMARK_UNSUPPORTED, reply_markup=back_to_menu())
        return

    user_id = message.from_user.id if message.from_user else 0
    if limiter is not None and not limiter.allow("media", user_id):
        await message.answer(texts.RATE_LIMITED, reply_markup=back_to_menu())
        return
    if incoming.exceeds(settings.input_limit_bytes):
        # Refused before a single byte is fetched; a smaller file can follow.
        await message.answer(texts.ERROR_TOO_LARGE, reply_markup=back_to_menu())
        return

    # Nothing is downloaded yet: the wizard only needs to remember which file.
    await state.set_data({"file": incoming.to_dict()})
    await state.set_state(WatermarkStates.choosing_type)
    await message.answer(texts.WATERMARK_TYPE_PROMPT_CHOICE, reply_markup=type_choices())


# --- text or logo -------------------------------------------------------------


@router.callback_query(WatermarkCallback.filter(F.action == "type"))
async def choose_type(
    callback: CallbackQuery,
    callback_data: WatermarkCallback,
    state: FSMContext,
    session: AsyncSession | None = None,
) -> None:
    """Two ways to brand a file: a line of text, or a picture."""
    if callback_data.value == "logo":
        await state.update_data(kind="logo")
        await state.set_state(WatermarkStates.waiting_for_logo)
        if session is not None and callback.from_user is not None:
            await EventsRepository(session).record(
                callback.from_user.id, Feature.WATERMARK_LOGO
            )
        await _replace(callback, texts.WATERMARK_LOGO_PROMPT, back_to_menu())
        return
    await state.update_data(kind="text")
    await state.set_state(WatermarkStates.choosing_source)
    await _replace(callback, texts.WATERMARK_ASK_TEXT, text_source_choices())


@router.message(WatermarkStates.waiting_for_logo)
async def handle_logo(
    message: Message,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    limiter: RateLimiter | None = None,
) -> None:
    """Fetch the logo now, check it really is a small image, and keep it.

    The bytes travel with the wizard rather than a file reference: the same
    bytes are what a preset would store, and what the encoder is handed.
    """
    incoming = extract_image_source(message)
    if incoming is None:
        await message.answer(texts.WATERMARK_LOGO_UNSUPPORTED, reply_markup=back_to_menu())
        return

    user_id = message.from_user.id if message.from_user else 0
    if limiter is not None and not limiter.allow("media", user_id):
        await message.answer(texts.RATE_LIMITED, reply_markup=back_to_menu())
        return
    if incoming.exceeds(MAX_LOGO_BYTES):
        # Refused before a single byte is fetched.
        await message.answer(texts.WATERMARK_LOGO_TOO_LARGE, reply_markup=back_to_menu())
        return

    job_id = uuid.uuid4()
    files = TelegramFileService.from_settings(bot, settings)
    service = _watermark_service(settings)
    try:
        async with job_workspace(settings.temp_root, job_id) as workspace:
            fetched = None
            try:
                fetched = await files.fetch(incoming, workspace, job_id=str(job_id))
                analysis = validate_logo(
                    await service.analyze(fetched.path, job_id=str(job_id))
                )
                payload = fetched.path.read_bytes()
            finally:
                await files.release(fetched, job_id=str(job_id))
    except MediaProcessingError as exc:
        await message.answer(_logo_failure_text(exc), reply_markup=back_to_menu())
        return
    except Exception:  # noqa: BLE001 - anything else is simply not a logo
        logger.warning("logo upload could not be read", extra={"job_id": str(job_id)})
        await message.answer(texts.WATERMARK_LOGO_UNSUPPORTED, reply_markup=back_to_menu())
        return

    await state.update_data(
        kind="logo",
        logo=base64.b64encode(payload).decode("ascii"),
        logo_format=analysis.format,
    )
    await state.set_state(WatermarkStates.choosing_position)
    await message.answer(texts.WATERMARK_POSITION_PROMPT, reply_markup=position_choices())


def _logo_failure_text(error: MediaProcessingError) -> str:
    if error.code is ProcessingErrorCode.TOO_LARGE:
        return texts.WATERMARK_LOGO_TOO_LARGE
    return texts.WATERMARK_LOGO_UNSUPPORTED


# --- the text ----------------------------------------------------------------


@router.callback_query(WatermarkCallback.filter(F.action == "enter"))
async def ask_for_text(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(WatermarkStates.typing_text)
    await _replace(callback, texts.WATERMARK_TYPE_PROMPT, back_to_menu())


@router.message(WatermarkStates.typing_text)
async def handle_text(
    message: Message,
    state: FSMContext,
    settings: Settings,
    limiter: RateLimiter | None = None,
) -> None:
    if extract_optimizer_source(message) is not None:
        # A second file instead of the text: start over with the new one.
        await handle_media(message, state, settings=settings, limiter=limiter)
        return

    text = clean_watermark_text(message.text or message.caption)
    if text is None:
        await message.answer(texts.WATERMARK_TEXT_REJECTED, reply_markup=back_to_menu())
        return

    await state.update_data(text=text)
    await state.set_state(WatermarkStates.choosing_position)
    await message.answer(texts.WATERMARK_POSITION_PROMPT, reply_markup=position_choices())


# --- the look ----------------------------------------------------------------


@router.callback_query(WatermarkStates.choosing_position, WatermarkCallback.filter(F.action == "pos"))
async def handle_position(
    callback: CallbackQuery, callback_data: WatermarkCallback, state: FSMContext
) -> None:
    if not _valid(Position, callback_data.value):
        await callback.answer(texts.ERROR_GENERIC, show_alert=True)
        return
    await state.update_data(position=callback_data.value)
    if await _is_logo(state):
        # A picture has no colour or shadow to choose - straight to the size.
        await state.set_state(WatermarkStates.choosing_size)
        await _replace(callback, texts.WATERMARK_SIZE_PROMPT, size_choices())
        return
    await state.set_state(WatermarkStates.choosing_style)
    await _replace(callback, texts.WATERMARK_STYLE_PROMPT, style_choices())


@router.callback_query(WatermarkStates.choosing_style, WatermarkCallback.filter(F.action == "style"))
async def handle_style(
    callback: CallbackQuery, callback_data: WatermarkCallback, state: FSMContext
) -> None:
    if not _valid(Style, callback_data.value):
        await callback.answer(texts.ERROR_GENERIC, show_alert=True)
        return
    await state.update_data(style=callback_data.value)
    await state.set_state(WatermarkStates.choosing_size)
    await _replace(callback, texts.WATERMARK_SIZE_PROMPT, size_choices())


@router.callback_query(WatermarkStates.choosing_size, WatermarkCallback.filter(F.action == "size"))
async def handle_size(
    callback: CallbackQuery, callback_data: WatermarkCallback, state: FSMContext
) -> None:
    if not _valid(Size, callback_data.value):
        await callback.answer(texts.ERROR_GENERIC, show_alert=True)
        return
    await state.update_data(size=callback_data.value)
    await state.set_state(WatermarkStates.choosing_opacity)
    await _replace(callback, texts.WATERMARK_OPACITY_PROMPT, opacity_choices(OPACITIES))


@router.callback_query(
    WatermarkStates.choosing_opacity, WatermarkCallback.filter(F.action == "opacity")
)
async def handle_opacity(
    callback: CallbackQuery, callback_data: WatermarkCallback, state: FSMContext
) -> None:
    if not callback_data.value.isdigit() or int(callback_data.value) not in OPACITIES:
        await callback.answer(texts.ERROR_GENERIC, show_alert=True)
        return
    await state.update_data(opacity=int(callback_data.value))
    await _show_confirmation(callback, state)


@router.callback_query(WatermarkStates.confirming, WatermarkCallback.filter(F.action == "change"))
async def change_look(callback: CallbackQuery, state: FSMContext) -> None:
    """Keep the text, pick the look again."""
    await state.set_state(WatermarkStates.choosing_position)
    await _replace(callback, texts.WATERMARK_POSITION_PROMPT, position_choices())


async def _show_confirmation(callback: CallbackQuery, state: FSMContext) -> None:
    if await _is_logo(state):
        logo = await _logo_spec(state)
        if logo is None:
            await state.clear()
            await callback.answer(texts.WATERMARK_CHOICE_EXPIRED, show_alert=True)
            return
        await state.set_state(WatermarkStates.confirming)
        await _replace(
            callback,
            texts.watermark_logo_summary(
                position=logo.position.value, size=logo.size.value, opacity=logo.opacity
            ),
            confirmation_choices(),
        )
        return

    spec = await _spec(state)
    if spec is None:
        await state.clear()
        await callback.answer(texts.WATERMARK_CHOICE_EXPIRED, show_alert=True)
        return
    await state.set_state(WatermarkStates.confirming)
    await _replace(callback, texts.watermark_summary(**spec.to_dict()), confirmation_choices())


async def _is_logo(state: FSMContext) -> bool:
    return (await state.get_data()).get("kind") == "logo"


async def _logo_spec(state: FSMContext) -> LogoSpec | None:
    """The look of a picture watermark - and proof the picture is still here."""
    data = await state.get_data()
    if not data.get("logo"):
        return None
    return LogoSpec.from_dict({
        "kind": "logo",
        "position": data.get("position", Position.BOTTOM_RIGHT.value),
        "size": data.get("size", Size.M.value),
        "opacity": data.get("opacity", 75),
    })


async def _logo_bytes(state: FSMContext) -> bytes | None:
    raw = (await state.get_data()).get("logo")
    if not raw:
        return None
    try:
        return base64.b64decode(raw)
    except (ValueError, TypeError):  # pragma: no cover - only a corrupted state
        return None


async def _spec(state: FSMContext) -> WatermarkSpec | None:
    data = await state.get_data()
    return WatermarkSpec.from_dict({
        "text": data.get("text"),
        "position": data.get("position", Position.BOTTOM_RIGHT.value),
        "style": data.get("style", Style.WHITE_SHADOW.value),
        "size": data.get("size", Size.M.value),
        "opacity": data.get("opacity", 75),
    })


def _valid(enum_type: type[enum.Enum], value: str) -> bool:
    try:
        enum_type(value)
    except ValueError:
        return False
    return True


# --- presets -----------------------------------------------------------------


@router.callback_query(WatermarkCallback.filter(F.action == "presets"))
async def show_presets(
    callback: CallbackQuery, session: AsyncSession | None = None
) -> None:
    await _render_presets(callback, session)


async def _render_presets(callback: CallbackQuery, session: AsyncSession | None) -> None:
    presets = await _presets(session, _user_id(callback))
    text = texts.WATERMARK_PRESETS_TITLE if presets else texts.WATERMARK_PRESETS_EMPTY
    await _replace(callback, text, preset_list(presets))


@router.callback_query(WatermarkCallback.filter(F.action == "detail"))
async def show_preset(
    callback: CallbackQuery,
    callback_data: WatermarkCallback,
    state: FSMContext,
    session: AsyncSession | None = None,
) -> None:
    preset = await _preset(session, callback, callback_data.value)
    if preset is None:
        await callback.answer(texts.WATERMARK_PRESET_GONE, show_alert=True)
        return
    data = await state.get_data()
    can_use = bool(data.get("file"))
    if preset.is_logo:
        await _replace(
            callback,
            texts.watermark_logo_preset_detail(
                name=preset.name, position=preset.position, size=preset.size,
                opacity=preset.opacity,
            ),
            preset_detail(preset.id, can_use=can_use, is_logo=True),
        )
        return
    await _replace(
        callback,
        texts.watermark_preset_detail(
            name=preset.name, text=preset.text, position=preset.position,
            style=preset.style, size=preset.size, opacity=preset.opacity,
        ),
        preset_detail(preset.id, can_use=can_use),
    )


@router.callback_query(WatermarkCallback.filter(F.action == "use"))
async def use_preset(
    callback: CallbackQuery,
    callback_data: WatermarkCallback,
    state: FSMContext,
    session: AsyncSession | None = None,
) -> None:
    preset = await _preset(session, callback, callback_data.value)
    if preset is None:
        await callback.answer(texts.WATERMARK_PRESET_GONE, show_alert=True)
        return
    if preset.is_logo:
        await state.update_data(
            kind="logo",
            logo=base64.b64encode(preset.logo or b"").decode("ascii"),
            logo_format=preset.logo_format or "png",
            position=preset.position, size=preset.size, opacity=preset.opacity,
        )
    else:
        await state.update_data(
            kind="text",
            text=preset.text, position=preset.position, style=preset.style,
            size=preset.size, opacity=preset.opacity,
        )
    await _show_confirmation(callback, state)


@router.callback_query(WatermarkStates.confirming, WatermarkCallback.filter(F.action == "save"))
async def save_preset(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession | None = None
) -> None:
    """Keep this watermark - the mark and its look - for next time."""
    if session is None:
        await callback.answer(texts.ERROR_GENERIC, show_alert=True)
        return
    if await _is_logo(state):
        await _save_logo_preset(callback, state, session)
        return

    spec = await _spec(state)
    if spec is None:
        await callback.answer(texts.ERROR_GENERIC, show_alert=True)
        return
    try:
        await WatermarkPresetsRepository(session).create(
            _user_id(callback),
            name=spec.text[:64],
            text=spec.text,
            position=spec.position.value,
            style=spec.style.value,
            size=spec.size.value,
            opacity=spec.opacity,
        )
    except PresetLimitReached:
        await callback.answer(texts.watermark_preset_limit(MAX_PRESETS_PER_USER), show_alert=True)
        return
    except DuplicateName:
        await callback.answer(texts.WATERMARK_PRESET_DUPLICATE, show_alert=True)
        return
    await callback.answer(texts.WATERMARK_PRESET_SAVED)


async def _save_logo_preset(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    """A logo has no text to name itself with, so it gets the next free name.

    The image itself is stored, because the container's disk does not survive a
    deploy and a preset that loses its logo is not a preset.
    """
    logo = await _logo_bytes(state)
    spec = await _logo_spec(state)
    if logo is None or spec is None:
        await callback.answer(texts.WATERMARK_LOGO_GONE, show_alert=True)
        return

    data = await state.get_data()
    repository = WatermarkPresetsRepository(session)
    user_id = _user_id(callback)
    for index in range(1, MAX_PRESETS_PER_USER + 1):
        try:
            await repository.create(
                user_id,
                name=texts.watermark_logo_preset_name(index),
                kind="logo",
                text="",
                logo=logo,
                logo_format=str(data.get("logo_format") or "png"),
                position=spec.position.value,
                style=Style.WHITE_SHADOW.value,
                size=spec.size.value,
                opacity=spec.opacity,
            )
        except DuplicateName:
            continue          # that number is taken; try the next one
        except PresetLimitReached:
            await callback.answer(
                texts.watermark_preset_limit(MAX_PRESETS_PER_USER), show_alert=True
            )
            return
        await callback.answer(texts.WATERMARK_LOGO_PRESET_SAVED)
        return
    await callback.answer(texts.watermark_preset_limit(MAX_PRESETS_PER_USER), show_alert=True)


@router.callback_query(WatermarkCallback.filter(F.action == "new"))
async def ask_for_new_preset(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(WatermarkStates.typing_new_preset)
    await _replace(callback, texts.WATERMARK_NEW_PRESET_PROMPT, back_to_menu())


@router.message(WatermarkStates.typing_new_preset)
async def handle_new_preset(
    message: Message, state: FSMContext, session: AsyncSession | None = None
) -> None:
    text = clean_watermark_text(message.text)
    if text is None:
        await message.answer(texts.WATERMARK_TEXT_REJECTED, reply_markup=back_to_menu())
        return
    if session is None:  # pragma: no cover - the middleware always supplies one
        await message.answer(texts.ERROR_GENERIC, reply_markup=back_to_menu())
        return
    user_id = message.from_user.id if message.from_user else 0
    try:
        await WatermarkPresetsRepository(session).create(
            user_id,
            name=text[:64], text=text,
            position=Position.BOTTOM_RIGHT.value, style=Style.WHITE_SHADOW.value,
            size=Size.M.value, opacity=75,
        )
    except PresetLimitReached:
        await message.answer(texts.watermark_preset_limit(MAX_PRESETS_PER_USER))
        return
    except DuplicateName:
        await message.answer(texts.WATERMARK_PRESET_DUPLICATE)
        return

    presets = await _presets(session, user_id)
    await _back_to_wizard_state(state)
    await message.answer(texts.WATERMARK_PRESETS_TITLE, reply_markup=preset_list(presets))


@router.callback_query(WatermarkCallback.filter(F.action == "rename"))
async def ask_for_rename(
    callback: CallbackQuery, callback_data: WatermarkCallback, state: FSMContext
) -> None:
    await state.update_data(preset_id=callback_data.value)
    await state.set_state(WatermarkStates.renaming_preset)
    await _replace(callback, texts.WATERMARK_RENAME_PROMPT, back_to_menu())


@router.callback_query(WatermarkCallback.filter(F.action == "edit"))
async def ask_for_new_text(
    callback: CallbackQuery, callback_data: WatermarkCallback, state: FSMContext
) -> None:
    await state.update_data(preset_id=callback_data.value)
    await state.set_state(WatermarkStates.retyping_preset_text)
    await _replace(callback, texts.WATERMARK_EDIT_TEXT_PROMPT, back_to_menu())


@router.message(
    StateFilter(WatermarkStates.renaming_preset, WatermarkStates.retyping_preset_text)
)
async def handle_preset_edit(
    message: Message, state: FSMContext, session: AsyncSession | None = None
) -> None:
    value = clean_watermark_text(message.text)
    if value is None:
        await message.answer(texts.WATERMARK_TEXT_REJECTED, reply_markup=back_to_menu())
        return

    data = await state.get_data()
    if session is None:  # pragma: no cover - the middleware always supplies one
        await message.answer(texts.ERROR_GENERIC, reply_markup=back_to_menu())
        return
    user_id = message.from_user.id if message.from_user else 0
    repository = WatermarkPresetsRepository(session)
    preset = await _lookup(repository, data.get("preset_id"), user_id)
    if preset is None:
        await _back_to_wizard_state(state)
        await message.answer(texts.WATERMARK_PRESET_GONE, reply_markup=back_to_menu())
        return

    renaming = await state.get_state() == WatermarkStates.renaming_preset.state
    try:
        if renaming:
            await repository.rename(preset, value[:64])
        else:
            await repository.set_text(preset, value)
    except DuplicateName:
        await message.answer(texts.WATERMARK_PRESET_DUPLICATE)
        return

    await _back_to_wizard_state(state)
    await message.answer(
        texts.WATERMARK_PRESET_UPDATED,
        reply_markup=preset_detail(preset.id, can_use=bool(data.get("file"))),
    )


@router.callback_query(WatermarkCallback.filter(F.action == "delete"))
async def confirm_delete(
    callback: CallbackQuery, callback_data: WatermarkCallback, session: AsyncSession | None = None
) -> None:
    preset = await _preset(session, callback, callback_data.value)
    if preset is None:
        await callback.answer(texts.WATERMARK_PRESET_GONE, show_alert=True)
        return
    await _replace(
        callback, texts.watermark_delete_confirmation(preset.name), delete_confirmation(preset.id)
    )


@router.callback_query(WatermarkCallback.filter(F.action == "delete_yes"))
async def delete_preset(
    callback: CallbackQuery,
    callback_data: WatermarkCallback,
    session: AsyncSession | None = None,
) -> None:
    preset = await _preset(session, callback, callback_data.value)
    if preset is not None:
        await WatermarkPresetsRepository(session).delete(preset)
    await callback.answer(texts.WATERMARK_PRESET_DELETED)
    await _render_presets(callback, session)


@router.callback_query(WatermarkCallback.filter(F.action == "back"))
async def leave_presets(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession | None = None
) -> None:
    """Back out of the preset list: to the wizard if a file is waiting."""
    data = await state.get_data()
    if data.get("file"):
        await state.set_state(WatermarkStates.choosing_type)
        await _replace(callback, texts.WATERMARK_TYPE_PROMPT_CHOICE, type_choices())
        return
    await state.clear()
    await _replace(callback, texts.MAIN_MENU, main_menu())


async def _presets(session: AsyncSession | None, user_id: int) -> list[WatermarkPreset]:
    if session is None:
        return []
    return await WatermarkPresetsRepository(session).list_for(user_id)


async def _preset(
    session: AsyncSession | None, callback: CallbackQuery, raw_id: str
) -> WatermarkPreset | None:
    if session is None:
        return None
    return await _lookup(WatermarkPresetsRepository(session), raw_id, _user_id(callback))


async def _lookup(
    repository: WatermarkPresetsRepository, raw_id: Any, user_id: int
) -> WatermarkPreset | None:
    try:
        preset_id = int(raw_id)
    except (TypeError, ValueError):
        return None
    # Scoped to the caller: one user can never reach another's preset.
    return await repository.get(preset_id, user_id)


async def _back_to_wizard_state(state: FSMContext) -> None:
    """After a preset edit, return to whichever flow the user was in."""
    data = await state.get_data()
    await state.set_state(
        WatermarkStates.choosing_type if data.get("file") else None
    )


def _user_id(callback: CallbackQuery) -> int:
    return callback.from_user.id if callback.from_user else 0


# --- applying ----------------------------------------------------------------


@router.callback_query(WatermarkStates.confirming, WatermarkCallback.filter(F.action == "apply"))
async def apply_watermark(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    session: AsyncSession,
    limiter: RateLimiter | None = None,
    user: User | None = None,
    media_gate: MediaJobGate | None = None,
) -> None:
    data = await state.get_data()
    incoming = IncomingFile.from_dict(data.get("file"))
    message = callback.message
    logo: bytes | None = None
    if await _is_logo(state):
        spec = await _logo_spec(state)
        logo = await _logo_bytes(state)
        if spec is not None and logo is None:
            await callback.answer(texts.WATERMARK_LOGO_GONE, show_alert=True)
            return
    else:
        spec = await _spec(state)
    if incoming is None or spec is None or not isinstance(message, Message):
        await state.clear()
        await callback.answer(texts.WATERMARK_CHOICE_EXPIRED, show_alert=True)
        return
    await callback.answer()

    user_id = _user_id(callback)
    if limiter is not None and not limiter.allow("media", user_id):
        await message.answer(texts.RATE_LIMITED, reply_markup=confirmation_choices())
        return

    # Leave the confirming state so a second tap cannot start a second job.
    await state.set_state(None)
    outcome = await _run_watermark_job(
        message=message, state=state, bot=bot, settings=settings, session=session,
        incoming=incoming, spec=spec, logo=logo,
        logo_format=str(data.get("logo_format") or "png"),
        user_id=user_id, user=user, media_gate=media_gate,
    )
    if outcome is not _Outcome.DONE:
        # The summary is still on screen: Apply can simply be tapped again.
        await state.set_state(WatermarkStates.confirming)


@router.callback_query(WatermarkCallback.filter(F.action == "cancel"))
async def cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _replace(callback, texts.MAIN_MENU, main_menu(), notice=texts.CANCELLED)


@router.callback_query(WatermarkCallback.filter(F.action == "apply"))
async def stale_apply(callback: CallbackQuery) -> None:
    """A tap on an old confirmation: the job already ran, or the bot restarted."""
    await callback.answer(texts.WATERMARK_CHOICE_EXPIRED, show_alert=True)


def _watermark_service(settings: Settings) -> WatermarkService:
    return WatermarkService(
        ffmpeg_bin=settings.ffmpeg_bin,
        ffprobe_bin=settings.ffprobe_bin,
        timeout=settings.process_timeout_seconds,
        probe_timeout=min(settings.process_timeout_seconds, 120),
        font=settings.watermark_font,
    )


def _is_heavy(media_gate: MediaJobGate | None, incoming: IncomingFile) -> bool:
    if media_gate is None:
        return False
    long_video = incoming.kind is FileKind.VIDEO and (incoming.duration or 0) > _LONG_VIDEO_SECONDS
    return media_gate.is_heavy(incoming.size) or long_video


async def _run_watermark_job(
    *,
    message: Message,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    session: AsyncSession,
    incoming: IncomingFile,
    spec: WatermarkSpec | LogoSpec,
    user_id: int,
    logo: bytes | None = None,
    logo_format: str = "png",
    user: User | None = None,
    media_gate: MediaJobGate | None = None,
) -> _Outcome:
    """Fetch, draw, verify and send back - always cleaning up afterwards."""
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

    async def record_job():
        return await jobs.create(
            job_id=job_id,
            telegram_user_id=user_id,
            job_type=JobType.WATERMARK,
            original_filename=incoming.original_filename,
            input_size=incoming.size,
        )

    status = await message.answer(texts.WATERMARK_PROCESSING)
    files = TelegramFileService.from_settings(bot, settings)
    service = _watermark_service(settings)

    try:
        with ticket:
            async with job_workspace(settings.temp_root, job_id) as workspace:
                fetched = None
                try:
                    fetched = await files.fetch(incoming, workspace, job_id=str(job_id))
                    analysis = await service.analyze(fetched.path, job_id=str(job_id))
                    job = await record_job()
                    await commit_early(session)

                    progress = ProgressReporter(status, render=texts.watermark_progress)
                    if isinstance(spec, LogoSpec):
                        # The mark is written into the workspace and removed
                        # with it, exactly like the file being watermarked.
                        mark = workspace.path / (
                            LOGO_FILENAME + IMAGE_EXTENSIONS.get(logo_format, ".png")
                        )
                        mark.write_bytes(logo or b"")
                        result = await service.apply_logo(
                            fetched.path, workspace, analysis, spec, mark,
                            job_id=str(job_id), on_progress=progress,
                        )
                    else:
                        result = await service.apply(
                            fetched.path, workspace, analysis, spec,
                            job_id=str(job_id), on_progress=progress,
                        )
                    ensure_sendable(result.size_bytes, settings.output_limit_bytes)
                    await _send_document(bot, message, result, _output_name(incoming, analysis))

                    await jobs.mark_success(job, output_size=result.size_bytes)
                    logger.info(
                        "watermark job succeeded (%s, %d bytes)",
                        result.kind.value, result.size_bytes, extra=log_extra,
                    )
                finally:
                    await files.release(fetched, job_id=str(job_id))
    except Exception as exc:  # noqa: BLE001 - mapped to friendly copy below
        if job is None:
            job = await record_job()
        await jobs.mark_failed(job, error_code=error_code(exc))
        if isinstance(exc, (MediaProcessingError, FileTooLargeError, OutputTooLargeError)):
            logger.warning("watermark job failed: %s", error_code(exc), extra=log_extra)
        else:
            logger.exception("watermark job failed", extra=log_extra)
        await message.answer(_failure_text(exc), reply_markup=back_to_menu())
        return _Outcome.FAILED
    finally:
        await delete_quietly(status)

    await state.clear()
    await message.answer(texts.WATERMARK_DONE, reply_markup=watermark_done())
    await maybe_send_cta(message, user)
    return _Outcome.DONE


def _output_name(incoming: IncomingFile, analysis: Any) -> str:
    image_format = None if isinstance(analysis, VideoAnalysis) else analysis.format
    return watermarked_filename(incoming.original_filename, analysis.kind, image_format)


async def _send_document(
    bot: Bot, message: Message, result: WatermarkedMedia, filename: str
) -> Any:
    """As a File, so Telegram does not recompress the watermark away."""
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


def _failure_text(error: BaseException) -> str:
    if isinstance(error, MediaProcessingError) and error.code in _FAILURE_TEXTS:
        return _FAILURE_TEXTS[error.code]
    return user_message(error)


async def _replace(callback: CallbackQuery, text: str, markup, *, notice: str | None = None) -> None:
    """Edit in place, falling back to a new message when Telegram refuses
    (an unchanged body, or a message too old to edit)."""
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_text(text, reply_markup=markup)
        except Exception:  # noqa: BLE001 - message may not be editable
            await callback.message.answer(text, reply_markup=markup)
    await callback.answer(notice)
