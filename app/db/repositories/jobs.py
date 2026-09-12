"""Processing job bookkeeping (analytics for success/failure rates)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Job, JobStatus, JobType


class JobsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        job_id: uuid.UUID,
        telegram_user_id: int,
        job_type: JobType,
        original_filename: str | None = None,
        input_size: int | None = None,
    ) -> Job:
        job = Job(
            id=job_id,
            telegram_user_id=telegram_user_id,
            type=job_type.value,
            status=JobStatus.PROCESSING.value,
            original_filename=(original_filename or None),
            input_size=input_size,
            created_at=datetime.now(timezone.utc),
        )
        self._session.add(job)
        await self._session.flush()
        return job

    async def mark_success(self, job: Job, *, output_size: int | None = None) -> Job:
        job.status = JobStatus.SUCCESS.value
        job.output_size = output_size
        job.finished_at = datetime.now(timezone.utc)
        await self._session.flush()
        return job

    async def mark_failed(self, job: Job, *, error_code: str) -> Job:
        job.status = JobStatus.FAILED.value
        job.error_code = error_code[:64]
        job.finished_at = datetime.now(timezone.utc)
        await self._session.flush()
        return job

    async def fail_interrupted(self, *, error_code: str = "interrupted") -> int:
        """Close out jobs a killed process left mid-flight.

        Run once at startup, before polling begins: nothing of ours is running
        yet, so anything still marked "processing" belongs to a previous
        process (a deploy, a restart, an OOM kill) and would otherwise sit in
        that state forever and skew the statistics.
        """
        result = await self._session.execute(
            update(Job)
            .where(Job.status == JobStatus.PROCESSING.value)
            .values(
                status=JobStatus.FAILED.value,
                error_code=error_code,
                finished_at=datetime.now(timezone.utc),
            )
        )
        await self._session.flush()
        return int(result.rowcount or 0)

    async def get(self, job_id: uuid.UUID) -> Job | None:
        return await self._session.get(Job, job_id)
