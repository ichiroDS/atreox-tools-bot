"""Application entrypoint."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, ErrorEvent, Message
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import texts
from app.bot.keyboards.common import main_menu
from app.bot.middlewares import DbSessionMiddleware, UserMiddleware
from app.bot.routers import build_root_router
from app.config import Settings, get_settings
from app.db.session import create_engine, create_session_factory
from app.logging_config import setup_logging
from app.services.jobgate import MediaJobGate
from app.services.ratelimit import RateLimiter
from app.startup import describe_database
from app.utils.temp_files import sweep_stale_workspaces

logger = logging.getLogger(__name__)


def create_bot(settings: Settings) -> Bot:
    """Bot bound to the configured API server.

    Pointing ``BOT_API_BASE_URL`` at a self-hosted Bot API server (plus
    ``BOT_API_FILES_URL`` when it runs in another container) is the only change
    needed to lift Telegram's cloud file-size limits. Incoming files are fetched
    by :class:`~app.services.telegram_files.TelegramFileService`, which handles
    both the cloud's relative paths and the local server's absolute ones.
    """
    api_server = TelegramAPIServer.from_base(
        settings.bot_api_base_url, is_local=settings.uses_local_bot_api
    )
    return Bot(
        token=settings.bot_token,
        session=AiohttpSession(api=api_server),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


async def handle_unexpected_error(event: ErrorEvent) -> bool:
    """Last line of defence: log the traceback, show the user friendly copy."""
    logger.exception("unhandled update error: %s", event.exception)
    message = getattr(event.update, "message", None) or getattr(
        getattr(event.update, "callback_query", None), "message", None
    )
    if isinstance(message, Message):
        try:
            await message.answer(texts.ERROR_GENERIC, reply_markup=main_menu())
        except Exception:  # noqa: BLE001 - nothing more we can do
            logger.warning("failed to deliver the error message to the user")
    return True


# The public command menu. /stats is deliberately absent: it is admin-only,
# and listing it would advertise a command most users cannot run.
PUBLIC_COMMANDS = (
    BotCommand(command="start", description="Open Atreox Tools"),
    BotCommand(command="help", description="How to use the tools"),
    BotCommand(command="privacy", description="Privacy information"),
)


async def register_commands(bot: Bot) -> None:
    """Publish the slash-command menu, best effort."""
    try:
        await bot.set_my_commands(list(PUBLIC_COMMANDS))
    except Exception:  # noqa: BLE001 - a missing menu must not stop the bot
        logger.warning("could not register the command menu")


def create_dispatcher(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> Dispatcher:
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher["settings"] = settings
    # Shared by every handler that guards an expensive path.
    dispatcher["limiter"] = RateLimiter()
    # CPU slots and disk accounting for media jobs.
    dispatcher["media_gate"] = MediaJobGate.from_settings(settings)

    dispatcher.update.outer_middleware(DbSessionMiddleware(session_factory))
    dispatcher.update.outer_middleware(UserMiddleware())

    root = build_root_router()
    root.errors.register(handle_unexpected_error)
    dispatcher.include_router(root)
    return dispatcher


async def run() -> None:
    settings = get_settings()
    setup_logging(settings.log_level_int)
    settings.temp_root.mkdir(parents=True, exist_ok=True)
    # Nothing is running yet, so anything under TEMP_ROOT is a leftover.
    sweep_stale_workspaces(settings.temp_root)

    # The token is never logged - only where we talk to, and to which database.
    logger.info(
        "application starting (api=%s, local_api=%s, files_url=%s, temp_root=%s)",
        settings.bot_api_base_url,
        settings.uses_local_bot_api,
        settings.bot_api_files_url or "-",
        settings.temp_root,
    )
    logger.info(
        "media limits: input=%dMB output=%dMB heavy_jobs=%d timeout=%ss",
        settings.input_limit_bytes // (1024 * 1024),
        settings.output_limit_bytes // (1024 * 1024),
        settings.max_concurrent_media_jobs,
        settings.process_timeout_seconds,
    )
    if settings.uses_local_bot_api and not settings.bot_api_files_url:
        logger.warning(
            "local Bot API without BOT_API_FILES_URL: files are only reachable "
            "if the server's %s is mounted into this container",
            settings.bot_api_local_dir,
        )

    engine = create_engine(settings.database_url, echo=settings.db_echo)
    session_factory = create_session_factory(engine)
    bot = create_bot(settings)
    dispatcher = create_dispatcher(settings, session_factory)

    try:
        # Fail loudly here rather than on the first user's update.
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        logger.info("database connected (%s)", describe_database(settings.database_url))

        await bot.delete_webhook(drop_pending_updates=True)
        await register_commands(bot)
        logger.info("starting Telegram long polling")
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()
        await engine.dispose()
        logger.info("stopped")


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):  # pragma: no cover - operator action
        logging.getLogger(__name__).info("interrupted")


if __name__ == "__main__":
    main()
