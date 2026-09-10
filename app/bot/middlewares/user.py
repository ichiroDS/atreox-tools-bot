"""Upserts the Telegram user behind every update and tracks activity."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User as TelegramUser

from app.db.repositories import TelegramIdentity, UsersRepository

logger = logging.getLogger(__name__)


class UserMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        telegram_user: TelegramUser | None = data.get("event_from_user")
        session = data.get("session")

        if telegram_user is not None and session is not None and not telegram_user.is_bot:
            repository = UsersRepository(session)
            user, created = await repository.upsert(
                TelegramIdentity(
                    telegram_user_id=telegram_user.id,
                    username=telegram_user.username,
                    first_name=telegram_user.first_name,
                    language_code=telegram_user.language_code,
                )
            )
            data["user"] = user
            data["is_new_user"] = created
            if created:
                logger.info("new user telegram_user_id=%s", telegram_user.id)

        return await handler(event, data)
