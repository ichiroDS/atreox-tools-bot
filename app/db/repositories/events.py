"""Feature-selection analytics."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Feature, FeatureEvent


class EventsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, telegram_user_id: int, feature: Feature) -> FeatureEvent:
        event = FeatureEvent(
            telegram_user_id=telegram_user_id,
            feature=feature.value,
            created_at=datetime.now(timezone.utc),
        )
        self._session.add(event)
        await self._session.flush()
        return event
