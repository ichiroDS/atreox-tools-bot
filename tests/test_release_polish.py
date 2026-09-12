"""Release-polish behaviour: attribution, CTA pacing, fair use and stats."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot import texts
from app.bot.promo import maybe_send_cta
from app.bot.routers import admin as admin_router
from app.bot.routers import start as start_router
from app.db.models import Feature, JobType, User
from app.db.repositories import (
    EventsRepository,
    JobsRepository,
    StatsRepository,
    TelegramIdentity,
    UsersRepository,
)
from app.services.cta import CtaPolicy, record_cta_shown, should_show_cta
from app.services.ratelimit import Limit, RateLimiter


class FakeMessage:
    def __init__(self, **fields):
        self.chat = SimpleNamespace(id=1)
        self.from_user = SimpleNamespace(id=42, is_bot=False)
        self.answers: list[str] = []
        self.markups: list = []
        for key, value in fields.items():
            setattr(self, key, value)

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)
        self.markups.append(kwargs.get("reply_markup"))
        return FakeMessage()


class FakeCallback:
    def __init__(self, message: FakeMessage):
        self.message = message
        self.from_user = SimpleNamespace(id=42, is_bot=False)
        self.answers: list = []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)


@pytest_asyncio.fixture
async def state():
    context = FSMContext(
        storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=42)
    )
    yield context
    await context.clear()


def make_user(**overrides) -> User:
    defaults = dict(
        telegram_user_id=42,
        created_at=datetime.now(timezone.utc),
        last_active_at=datetime.now(timezone.utc),
        cta_shown_count=0,
        cta_last_shown_at=None,
    )
    return User(**{**defaults, **overrides})


# --- deep link attribution --------------------------------------------------


async def test_first_start_records_the_deep_link_source(state):
    user = make_user(source=None)
    message = FakeMessage()
    await start_router.handle_start(
        message,
        SimpleNamespace(args="atreox_channel"),
        state,
        user=user,
        is_new_user=True,
    )
    assert user.source == "atreox_channel"


async def test_start_without_a_payload_attributes_to_direct(state):
    user = make_user(source=None)
    await start_router.handle_start(
        FakeMessage(), SimpleNamespace(args=None), state, user=user, is_new_user=True
    )
    assert user.source == "direct"


async def test_a_later_deep_link_never_overwrites_the_original_source(state):
    user = make_user(source="website")
    for payload in ("discord", "atreox_channel", None):
        await start_router.handle_start(
            FakeMessage(),
            SimpleNamespace(args=payload),
            state,
            user=user,
            is_new_user=False,
        )
    assert user.source == "website"


async def test_a_user_created_before_their_first_start_still_gets_attributed(state):
    # The middleware creates the row before the handler runs, so source is
    # blank on the very first update even for a brand new user.
    user = make_user(source=None)
    await start_router.handle_start(
        FakeMessage(),
        SimpleNamespace(args="discord"),
        state,
        user=user,
        is_new_user=False,
    )
    assert user.source == "discord"


async def test_start_survives_without_a_user_row(state):
    message = FakeMessage()
    await start_router.handle_start(
        message, SimpleNamespace(args="website"), state, user=None
    )
    assert message.answers == [texts.START]


async def test_source_is_persisted_and_kept_on_a_second_visit(session):
    users = UsersRepository(session)
    user, created = await users.upsert(
        TelegramIdentity(telegram_user_id=7, username="a"), source="website"
    )
    assert created and user.source == "website"

    again, created_again = await users.upsert(
        TelegramIdentity(telegram_user_id=7, username="a"), source="discord"
    )
    assert not created_again
    assert again.source == "website"


# --- CTA pacing -------------------------------------------------------------


def test_cta_appears_after_the_very_first_success():
    assert should_show_cta(make_user(cta_shown_count=0)) is True


def test_cta_does_not_repeat_straight_away():
    now = datetime.now(timezone.utc)
    user = make_user(cta_shown_count=1, cta_last_shown_at=now)
    assert should_show_cta(user, now=now + timedelta(hours=6)) is False
    assert should_show_cta(user, now=now + timedelta(days=3)) is False


def test_cta_returns_after_the_quiet_period():
    now = datetime.now(timezone.utc)
    user = make_user(cta_shown_count=1, cta_last_shown_at=now)
    assert should_show_cta(user, now=now + timedelta(days=8)) is True


def test_cta_stops_for_good_once_the_cap_is_reached():
    long_ago = datetime.now(timezone.utc) - timedelta(days=400)
    user = make_user(cta_shown_count=CtaPolicy().max_shows, cta_last_shown_at=long_ago)
    assert should_show_cta(user) is False


def test_cta_handles_a_naive_timestamp_from_sqlite():
    naive = datetime.now(timezone.utc).replace(tzinfo=None)
    user = make_user(cta_shown_count=1, cta_last_shown_at=naive)
    # Must not raise comparing naive and aware datetimes.
    assert should_show_cta(user) is False


def test_cta_is_skipped_when_there_is_no_user():
    assert should_show_cta(None) is False


def test_recording_a_show_advances_the_counter():
    user = make_user()
    record_cta_shown(user)
    assert user.cta_shown_count == 1
    assert user.cta_last_shown_at is not None


async def test_maybe_send_cta_delivers_once_then_goes_quiet():
    user = make_user()
    message = FakeMessage()

    assert await maybe_send_cta(message, user) is True
    assert message.answers == [texts.CTA]
    # A second job right afterwards must not be followed by another promo.
    assert await maybe_send_cta(message, user) is False
    assert message.answers == [texts.CTA]


async def test_a_failing_cta_never_breaks_the_flow():
    class Exploding(FakeMessage):
        async def answer(self, text=None, **kwargs):
            raise RuntimeError("telegram is down")

    user = make_user()
    assert await maybe_send_cta(Exploding(), user) is False
    # Nothing was delivered, so nothing is counted against the user.
    assert user.cta_shown_count == 0


async def test_cta_click_is_recorded_as_an_event(session):
    callback = FakeCallback(FakeMessage())
    await start_router.handle_cta(callback, session=session)

    stats = await StatsRepository(session).collect()
    assert stats.cta_clicks == 1


# --- fair use ---------------------------------------------------------------


def test_limiter_allows_a_normal_burst_then_pushes_back():
    limiter = RateLimiter({"media": Limit(count=3, per_seconds=60)})
    assert [limiter.allow("media", 1, now=t) for t in (0, 1, 2)] == [True] * 3
    assert limiter.allow("media", 1, now=3) is False


def test_the_window_slides_so_nobody_is_stuck():
    limiter = RateLimiter({"media": Limit(count=2, per_seconds=10)})
    limiter.allow("media", 1, now=0)
    limiter.allow("media", 1, now=1)
    assert limiter.allow("media", 1, now=2) is False
    # Once the oldest hit ages out the user is free again - never a ban.
    assert limiter.allow("media", 1, now=11) is True


def test_limits_are_per_user_and_per_bucket():
    limiter = RateLimiter({"media": Limit(count=1, per_seconds=60)})
    assert limiter.allow("media", 1, now=0) is True
    assert limiter.allow("media", 1, now=0) is False
    # A different user is unaffected...
    assert limiter.allow("media", 2, now=0) is True
    # ...and an unlimited bucket never blocks.
    assert limiter.allow("unknown", 1, now=0) is True


def test_default_limits_leave_real_usage_alone():
    limiter = RateLimiter()
    # A dozen conversions inside a minute is already heavy manual use, and the
    # job gate - not this - is what bounds the expensive work.
    assert all(limiter.allow("media", 1, now=i * 4) for i in range(12))
    assert limiter.allow("media", 1, now=45) is False
    # Collecting a batch uses its own, roomier bucket.
    assert all(limiter.allow("batch_upload", 1, now=i) for i in range(40))


def test_reset_clears_history():
    limiter = RateLimiter({"media": Limit(count=1, per_seconds=60)})
    limiter.allow("media", 1, now=0)
    limiter.reset("media", 1)
    assert limiter.allow("media", 1, now=0) is True


# --- admin stats ------------------------------------------------------------


async def seed_activity(session) -> None:
    users = UsersRepository(session)
    for index, source in enumerate(("website", "website", "discord", None), start=1):
        await users.upsert(
            TelegramIdentity(telegram_user_id=100 + index), source=source
        )

    jobs = JobsRepository(session)
    for job_type in (JobType.CIRCLE, JobType.CIRCLE, JobType.METADATA_CLEAN):
        job = await jobs.create(
            job_id=uuid.uuid4(), telegram_user_id=101, job_type=job_type
        )
        await jobs.mark_success(job)
    failed = await jobs.create(
        job_id=uuid.uuid4(), telegram_user_id=101, job_type=JobType.METADATA_CHANGE
    )
    await jobs.mark_failed(failed, error_code="metadata_failed")

    events = EventsRepository(session)
    await events.record(101, Feature.STICKERS)
    await events.record(102, Feature.STICKERS)
    await events.record(101, Feature.CTA_CLICK)


async def test_stats_count_users_jobs_and_features(session):
    await seed_activity(session)
    stats = await StatsRepository(session).collect()

    assert stats.users.total == 4
    assert stats.users.today == 4
    assert stats.jobs.total == 4
    assert stats.circles == 2
    assert stats.metadata_cleans == 1
    # Only successful jobs count towards a tool's tally.
    assert stats.metadata_changes == 0
    assert stats.sticker_searches == 2
    assert stats.cta_clicks == 1


async def test_stats_rank_acquisition_sources(session):
    await seed_activity(session)
    stats = await StatsRepository(session).collect()
    assert stats.top_sources[0] == ("website", 2)
    assert ("discord", 1) in stats.top_sources
    # A user with no source is simply not attributed.
    assert all(source for source, _ in stats.top_sources)


async def test_stats_report_renders_on_an_empty_database(session):
    stats = await StatsRepository(session).collect()
    report = texts.stats_report(stats)
    assert "Atreox Tools" in report
    assert "No attributed users yet." in report


async def test_stats_command_answers_an_admin(session, state):
    message = FakeMessage()
    await admin_router.show_stats(message, state, session)
    assert "Atreox Tools" in message.answers[0]


@pytest.mark.parametrize("user_id,expected", [(42, True), (7, False)])
async def test_only_configured_admins_pass_the_stats_filter(user_id, expected, clean_env):
    from app.config import Settings

    settings = Settings(bot_token="1:a", admin_user_ids="42")
    event = SimpleNamespace(from_user=SimpleNamespace(id=user_id))
    assert await admin_router.IsAdmin()(event, settings=settings) is expected


async def test_stats_is_not_in_the_public_command_menu():
    from app.main import PUBLIC_COMMANDS

    assert "stats" not in {command.command for command in PUBLIC_COMMANDS}
