from __future__ import annotations

import random
from datetime import date, timedelta, timezone

import pytest

from app.services.timeofday import (
    INTERVALS,
    format_interval,
    LongitudeOffsetTimezoneResolver,
    TimeOfDay,
    UtcTimezoneResolver,
    contains,
    interval_for,
    pick_datetime,
)

TODAY = date(2026, 5, 17)


@pytest.mark.parametrize(
    "time_of_day,expected",
    [
        (TimeOfDay.MORNING, "07:00-11:29"),
        (TimeOfDay.DAY, "11:30-16:59"),
        (TimeOfDay.EVENING, "17:00-21:59"),
        (TimeOfDay.NIGHT, "22:00-02:59"),
    ],
)
def test_intervals_match_the_specified_ranges(time_of_day, expected):
    assert format_interval(time_of_day) == expected


def test_intervals_do_not_overlap():
    # Every minute belongs to at most one interval.
    for minute in range(24 * 60):
        hour, rest = divmod(minute, 60)
        matches = [t for t in TimeOfDay if contains(t, hour, rest)]
        assert len(matches) <= 1, (minute, matches)


@pytest.mark.parametrize(
    "time_of_day,hour,minute",
    [
        (TimeOfDay.MORNING, 7, 0),
        (TimeOfDay.MORNING, 11, 29),
        (TimeOfDay.DAY, 11, 30),
        (TimeOfDay.DAY, 16, 59),
        (TimeOfDay.EVENING, 17, 0),
        (TimeOfDay.EVENING, 21, 59),
        (TimeOfDay.NIGHT, 22, 0),
        (TimeOfDay.NIGHT, 2, 59),
    ],
)
def test_contains_at_the_interval_edges(time_of_day, hour, minute):
    assert contains(time_of_day, hour, minute)


@pytest.mark.parametrize(
    "time_of_day,hour,minute",
    [
        (TimeOfDay.MORNING, 6, 59),
        (TimeOfDay.MORNING, 11, 30),
        (TimeOfDay.DAY, 11, 29),
        (TimeOfDay.DAY, 17, 0),
        (TimeOfDay.EVENING, 22, 0),
        (TimeOfDay.NIGHT, 3, 0),
        # 03:00-06:59 deliberately belongs to no interval.
        (TimeOfDay.NIGHT, 5, 0),
    ],
)
def test_contains_rejects_times_outside_the_interval(time_of_day, hour, minute):
    assert not contains(time_of_day, hour, minute)


@pytest.mark.parametrize("time_of_day", list(TimeOfDay))
@pytest.mark.parametrize("seed", range(20))
def test_picked_timestamp_falls_inside_the_interval(time_of_day, seed):
    picked = pick_datetime(
        time_of_day, on_date=TODAY, tz=timezone.utc, rng=random.Random(seed)
    )
    assert contains(time_of_day, picked.hour, picked.minute)
    assert picked.tzinfo is timezone.utc


def test_night_can_roll_over_into_the_next_day():
    seen_days = set()
    for seed in range(60):
        picked = pick_datetime(
            TimeOfDay.NIGHT, on_date=TODAY, tz=timezone.utc, rng=random.Random(seed)
        )
        seen_days.add(picked.date())
        start, end = interval_for(TimeOfDay.NIGHT)
        minute_of_day = picked.hour * 60 + picked.minute
        if picked.date() > TODAY:
            minute_of_day += 24 * 60
        assert start <= minute_of_day < end
    assert seen_days == {TODAY, TODAY + timedelta(days=1)}


def test_timestamp_uses_the_resolved_timezone():
    tz = LongitudeOffsetTimezoneResolver().resolve(50.45, 30.52)  # Kyiv
    picked = pick_datetime(TimeOfDay.DAY, on_date=TODAY, tz=tz, rng=random.Random(0))
    assert picked.utcoffset() == timedelta(hours=2)


@pytest.mark.parametrize(
    "longitude,hours",
    [(0.0, 0), (30.5, 2), (-74.0, -5), (139.7, 9), (-180.0, -12), (180.0, 12)],
)
def test_longitude_offset_resolver(longitude, hours):
    tz = LongitudeOffsetTimezoneResolver().resolve(0.0, longitude)
    assert tz.utcoffset(None) == timedelta(hours=hours)


def test_utc_resolver_is_always_utc():
    assert UtcTimezoneResolver().resolve(12.0, 34.0) is timezone.utc
