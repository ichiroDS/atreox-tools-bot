from __future__ import annotations

import uuid

from app.db.models import Feature, JobStatus, JobType, StickerSample, StickerSet
from app.db.repositories import (
    EventsRepository,
    JobsRepository,
    StickersRepository,
    TelegramIdentity,
    UsersRepository,
)


def identity(**overrides):
    fields = {
        "telegram_user_id": 4242,
        "username": "creator",
        "first_name": "Dana",
        "language_code": "en",
    }
    fields.update(overrides)
    return TelegramIdentity(**fields)


async def test_upsert_creates_then_reuses_the_same_row(session):
    repository = UsersRepository(session)

    user, created = await repository.upsert(identity(), source="tiktok")
    assert created is True
    assert user.source == "tiktok"

    same_user, created_again = await repository.upsert(identity(username="renamed"))
    assert created_again is False
    assert same_user.id == user.id
    assert same_user.username == "renamed"


async def test_acquisition_source_is_only_stored_on_first_contact(session):
    repository = UsersRepository(session)
    await repository.upsert(identity(), source="tiktok")

    user, created = await repository.upsert(identity(), source="instagram")
    assert created is False
    assert user.source == "tiktok"


async def test_user_without_a_deep_link_has_no_source(session):
    user, _ = await UsersRepository(session).upsert(identity())
    assert user.source is None


async def test_last_active_at_moves_forward(session):
    repository = UsersRepository(session)
    user, _ = await repository.upsert(identity())
    first_seen = user.last_active_at
    refreshed, _ = await repository.upsert(identity())
    assert refreshed.last_active_at >= first_seen


async def test_job_lifecycle_success(session):
    repository = JobsRepository(session)
    job_id = uuid.uuid4()
    job = await repository.create(
        job_id=job_id,
        telegram_user_id=4242,
        job_type=JobType.CIRCLE,
        original_filename="holiday.mp4",
        input_size=1000,
    )
    assert job.status == JobStatus.PROCESSING.value

    await repository.mark_success(job, output_size=800)
    stored = await repository.get(job_id)
    assert stored.status == JobStatus.SUCCESS.value
    assert stored.output_size == 800
    assert stored.finished_at is not None


async def test_job_lifecycle_failure_records_the_error_code(session):
    repository = JobsRepository(session)
    job = await repository.create(
        job_id=uuid.uuid4(), telegram_user_id=1, job_type=JobType.METADATA_CLEAN
    )
    await repository.mark_failed(job, error_code="processing_timeout")
    assert job.status == JobStatus.FAILED.value
    assert job.error_code == "processing_timeout"


async def test_feature_events_are_recorded(session):
    event = await EventsRepository(session).record(4242, Feature.STICKERS)
    assert event.feature == "stickers"
    assert event.telegram_user_id == 4242


async def test_sticker_repository_returns_only_enabled_sets_of_a_category(session):
    session.add_all(
        [
            StickerSet(
                telegram_set_name="cute_a",
                title="Cute A",
                category="cute",
                enabled=True,
                samples=[StickerSample(telegram_file_id="f1", emoji="🥰")],
            ),
            StickerSet(
                telegram_set_name="cute_off",
                title="Cute Off",
                category="cute",
                enabled=False,
                samples=[StickerSample(telegram_file_id="f2")],
            ),
            StickerSet(
                telegram_set_name="savage_a",
                title="Savage A",
                category="savage",
                enabled=True,
                samples=[StickerSample(telegram_file_id="f3")],
            ),
        ]
    )
    await session.flush()

    repository = StickersRepository(session)
    cute = await repository.enabled_sets_for_category("cute")
    assert [s.telegram_set_name for s in cute] == ["cute_a"]
    assert cute[0].samples[0].telegram_file_id == "f1"
    assert await repository.count_enabled_sets("savage") == 1
    assert await repository.count_enabled_sets("flirty") == 0
