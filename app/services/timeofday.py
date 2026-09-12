"""Time-of-day intervals and the timezone abstraction behind them.

The "Change Metadata" wizard only asks for a coarse time of day; the service
turns that into a plausible exact timestamp inside the chosen interval, in the
timezone implied by the supplied coordinates.
"""

from __future__ import annotations

import enum
import random
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Protocol


class TimeOfDay(str, enum.Enum):
    MORNING = "morning"
    DAY = "day"
    EVENING = "evening"
    NIGHT = "night"


# Half-open [start, end) intervals as minutes from local midnight. NIGHT
# deliberately wraps past midnight, expressed as minutes >= 24*60 and
# normalised in ``pick_datetime``.
#
#   Morning 07:00-11:29   Day 11:30-16:59
#   Evening 17:00-21:59   Night 22:00-02:59
INTERVALS: dict[TimeOfDay, tuple[int, int]] = {
    TimeOfDay.MORNING: (7 * 60, 11 * 60 + 30),
    TimeOfDay.DAY: (11 * 60 + 30, 17 * 60),
    TimeOfDay.EVENING: (17 * 60, 22 * 60),
    TimeOfDay.NIGHT: (22 * 60, 27 * 60),
}


def interval_for(time_of_day: TimeOfDay) -> tuple[int, int]:
    """The interval in minutes from local midnight."""
    return INTERVALS[time_of_day]


def format_interval(time_of_day: TimeOfDay) -> str:
    """Human readable range, e.g. ``07:00-11:29``."""
    start, end = interval_for(time_of_day)
    last = (end - 1) % (24 * 60)
    return f"{start // 60 % 24:02d}:{start % 60:02d}-{last // 60:02d}:{last % 60:02d}"


def contains(time_of_day: TimeOfDay, hour: int, minute: int = 0) -> bool:
    """Whether a local wall-clock time falls inside the interval."""
    start, end = interval_for(time_of_day)
    minute_of_day = (hour % 24) * 60 + minute
    # Compare both same-day and next-day positions so wrapping works.
    return any(
        start <= candidate < end
        for candidate in (minute_of_day, minute_of_day + 24 * 60)
    )


class TimezoneResolver(Protocol):
    """Maps coordinates to a timezone.

    V1 ships a coarse longitude-based implementation; swapping in
    ``timezonefinder`` later means providing another class with this signature.
    """

    def resolve(self, latitude: float, longitude: float) -> tzinfo: ...


class UtcTimezoneResolver:
    """Always UTC - used in tests and as a safe fallback."""

    def resolve(self, latitude: float, longitude: float) -> tzinfo:  # noqa: ARG002
        return timezone.utc


class LongitudeOffsetTimezoneResolver:
    """Approximates the local zone as a fixed offset of ``round(lon / 15)`` hours.

    Good enough for a plausible timestamp, and it never needs a data file.
    """

    def resolve(self, latitude: float, longitude: float) -> tzinfo:  # noqa: ARG002
        hours = max(-12, min(14, round(longitude / 15.0)))
        return timezone(timedelta(hours=hours))


def pick_datetime(
    time_of_day: TimeOfDay,
    *,
    on_date: date_cls,
    tz: tzinfo,
    rng: random.Random | None = None,
    not_after: datetime | None = None,
) -> datetime:
    """Return a random timezone-aware timestamp inside the interval.

    ``not_after`` - normally "now" - keeps the result in the past, and the
    caller should always pass it. The chosen interval may not have arrived yet
    on the chosen day: ask for "Night" (22:00) while it is 08:00 in Los
    Angeles and the naive answer is thirteen hours away, and "Night" also
    wraps past midnight, so it can land a further day out. A capture time in
    the future is the one timestamp no real recording can carry - the file
    would claim to have been shot after it already existed - so whole days are
    stepped back until the moment has actually happened.
    """
    rng = rng or random.Random()
    start, end = interval_for(time_of_day)

    minute_of_day = rng.randrange(start, end)
    day_offset, minute_of_day = divmod(minute_of_day, 24 * 60)
    hour, minute = divmod(minute_of_day, 60)

    naive = datetime(
        on_date.year, on_date.month, on_date.day, hour, minute, rng.randrange(60)
    ) + timedelta(days=day_offset)
    taken = naive.replace(tzinfo=tz)

    # A whole day at a time, so the local wall-clock time - the one thing the
    # user actually chose - stays inside the interval.
    while not_after is not None and taken > not_after:
        taken -= timedelta(days=1)
    return taken
