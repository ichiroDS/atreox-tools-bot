"""Aggregate counters for the admin /stats command.

Everything is derived from the tables the bot already writes - no separate
analytics store, no extra bookkeeping on the hot path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Feature, FeatureEvent, Job, JobStatus, JobType, User


@dataclass(frozen=True)
class Window:
    """The same three horizons for every counter."""

    today: int = 0
    week: int = 0
    total: int = 0


@dataclass(frozen=True)
class Stats:
    users: Window = field(default_factory=Window)
    jobs: Window = field(default_factory=Window)
    circles: int = 0
    voice_notes: int = 0
    # Optimizations that actually delivered a smaller file.
    optimizations: int = 0
    watermarks: int = 0
    metadata_cleans: int = 0
    metadata_changes: int = 0
    sticker_searches: int = 0
    cta_clicks: int = 0
    # (source, count), most popular first.
    top_sources: tuple[tuple[str, int], ...] = ()


def _boundaries(now: datetime) -> tuple[datetime, datetime]:
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_of_day, now - timedelta(days=7)


class StatsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _count(self, column, *conditions) -> int:
        statement = select(func.count()).select_from(column)
        for condition in conditions:
            statement = statement.where(condition)
        return int(await self._session.scalar(statement) or 0)

    async def _window(self, model, timestamp_column, *conditions) -> Window:
        now = datetime.now(timezone.utc)
        today_start, week_start = _boundaries(now)
        return Window(
            today=await self._count(model, timestamp_column >= today_start, *conditions),
            week=await self._count(model, timestamp_column >= week_start, *conditions),
            total=await self._count(model, *conditions),
        )

    async def collect(self, *, top_sources: int = 3) -> Stats:
        users = await self._window(User, User.created_at)
        jobs = await self._window(Job, Job.created_at)

        async def successful(job_type: JobType) -> int:
            return await self._count(
                Job,
                Job.type == job_type.value,
                Job.status == JobStatus.SUCCESS.value,
            )

        async def events(feature: Feature) -> int:
            return await self._count(FeatureEvent, FeatureEvent.feature == feature.value)

        sources = await self._session.execute(
            select(User.source, func.count())
            .where(User.source.is_not(None))
            .group_by(User.source)
            .order_by(func.count().desc())
            .limit(top_sources)
        )

        return Stats(
            users=users,
            jobs=jobs,
            circles=await successful(JobType.CIRCLE),
            voice_notes=await successful(JobType.VOICE_NOTE),
            # A job that found the file already efficient succeeds without an
            # output; it is not counted as an optimization.
            optimizations=await self._count(
                Job,
                Job.type == JobType.MEDIA_OPTIMIZE.value,
                Job.status == JobStatus.SUCCESS.value,
                Job.output_size.is_not(None),
            ),
            watermarks=await successful(JobType.WATERMARK),
            metadata_cleans=await successful(JobType.METADATA_CLEAN),
            metadata_changes=await successful(JobType.METADATA_CHANGE),
            sticker_searches=await events(Feature.STICKERS),
            cta_clicks=await events(Feature.CTA_CLICK),
            top_sources=tuple((str(source), int(count)) for source, count in sources),
        )
