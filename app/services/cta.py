"""When to show the AtreoxAI call to action.

The promo earns its place only if it is rare. The rule is deliberately
conservative: once after a user's first successful job, then at most once a
week and never more than a handful of times in total. Pacing state lives on the
``users`` row, so it survives restarts and is shared across every flow.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.db.models import User


@dataclass(frozen=True)
class CtaPolicy:
    """Tunable frequency caps."""

    # Minimum gap between two appearances, after the introductory one.
    min_interval: timedelta = timedelta(days=7)
    # Hard ceiling: past this the user has seen it enough.
    max_shows: int = 4


DEFAULT_POLICY = CtaPolicy()


def _as_utc(moment: datetime) -> datetime:
    """SQLite hands back naive datetimes; treat those as UTC."""
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def should_show_cta(
    user: User | None,
    *,
    now: datetime | None = None,
    policy: CtaPolicy = DEFAULT_POLICY,
) -> bool:
    """Whether the promo may follow the job that just succeeded."""
    if user is None:
        return False

    shown = user.cta_shown_count or 0
    if shown >= policy.max_shows:
        return False
    # The introductory one: straight after the first successful job.
    if shown == 0:
        return True

    last = user.cta_last_shown_at
    if last is None:
        return True
    now = now or datetime.now(timezone.utc)
    return now - _as_utc(last) >= policy.min_interval


def record_cta_shown(user: User | None, *, now: datetime | None = None) -> None:
    """Mark the promo as delivered, so the next one is a week away."""
    if user is None:
        return
    user.cta_shown_count = (user.cta_shown_count or 0) + 1
    user.cta_last_shown_at = now or datetime.now(timezone.utc)
