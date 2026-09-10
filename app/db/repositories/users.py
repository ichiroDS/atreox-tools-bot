"""User acquisition + activity tracking."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User


@dataclass(frozen=True)
class TelegramIdentity:
    """The subset of a Telegram user we persist."""

    telegram_user_id: int
    username: str | None = None
    first_name: str | None = None
    language_code: str | None = None


class UsersRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, telegram_user_id: int) -> User | None:
        return await self._session.scalar(
            select(User).where(User.telegram_user_id == telegram_user_id)
        )

    async def upsert(
        self, identity: TelegramIdentity, *, source: str | None = None
    ) -> tuple[User, bool]:
        """Create or refresh a user. Returns ``(user, created)``.

        ``source`` is only written on first acquisition - a user who comes back
        through a different deep link keeps the link that originally brought
        them in.
        """
        user = await self.get(identity.telegram_user_id)
        now = datetime.now(timezone.utc)
        created = False

        if user is None:
            user = User(
                telegram_user_id=identity.telegram_user_id,
                username=identity.username,
                first_name=identity.first_name,
                language_code=identity.language_code,
                source=source,
                created_at=now,
                last_active_at=now,
            )
            self._session.add(user)
            created = True
        else:
            user.username = identity.username
            user.first_name = identity.first_name
            user.language_code = identity.language_code
            user.last_active_at = now

        await self._session.flush()
        return user, created
