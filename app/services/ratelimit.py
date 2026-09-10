"""Fair-use protection for the expensive paths.

A sliding window per (bucket, user). The limits are set well above what a
person clicking through the bot can reach, so they only bite on automated
hammering - and they never ban: once the window drains, the user is free again.

In-process state is the right scope here: the bot runs as a single polling
process, and a limiter that forgets everything on restart is exactly what a
non-punitive fair-use guard should do.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Limit:
    """At most ``count`` events per ``per_seconds`` for one user."""

    count: int
    per_seconds: float


# Media jobs cost CPU and disk; sticker batches cost a burst of API calls.
DEFAULT_LIMITS: dict[str, Limit] = {
    # A real user converting clips back to back stays well under this.
    "media": Limit(count=8, per_seconds=60.0),
    "stickers": Limit(count=12, per_seconds=60.0),
}

# Stops the per-user deques growing without bound on a busy bot.
_MAX_TRACKED_USERS = 10_000


class RateLimiter:
    """Sliding-window limiter keyed by bucket and user."""

    def __init__(self, limits: dict[str, Limit] | None = None) -> None:
        self._limits = dict(limits or DEFAULT_LIMITS)
        self._hits: dict[tuple[str, int], deque[float]] = defaultdict(deque)

    def allow(self, bucket: str, user_id: int, *, now: float | None = None) -> bool:
        """Record an attempt and report whether it is within the limit."""
        limit = self._limits.get(bucket)
        if limit is None:
            return True

        now = time.monotonic() if now is None else now
        hits = self._hits[(bucket, user_id)]

        cutoff = now - limit.per_seconds
        while hits and hits[0] <= cutoff:
            hits.popleft()

        if len(hits) >= limit.count:
            logger.info(
                "rate limit hit bucket=%s telegram_user_id=%s", bucket, user_id
            )
            return False

        hits.append(now)
        self._forget_idle(now)
        return True

    def reset(self, bucket: str | None = None, user_id: int | None = None) -> None:
        """Clear tracked history - used by tests and by /cancel style resets."""
        if bucket is None and user_id is None:
            self._hits.clear()
            return
        for key in [
            k
            for k in self._hits
            if (bucket is None or k[0] == bucket) and (user_id is None or k[1] == user_id)
        ]:
            del self._hits[key]

    def _forget_idle(self, now: float) -> None:
        if len(self._hits) <= _MAX_TRACKED_USERS:
            return
        for key, hits in list(self._hits.items()):
            limit = self._limits.get(key[0])
            if not hits or (limit and hits[-1] <= now - limit.per_seconds):
                del self._hits[key]
