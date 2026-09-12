"""Plumbing shared by the media routers.

Long jobs must not hold on to anything they do not need: the database
connection is handed back as soon as the job row exists, and uploads get a
timeout sized to the file rather than aiogram's one-minute default.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from aiogram.exceptions import TelegramRetryAfter

from app.services.jobgate import JobTicket, MediaJobGate
from app.services.telegram_files import transfer_timeout

logger = logging.getLogger(__name__)

# How often a flood-wait from Telegram is honoured before giving up.
_MAX_FLOOD_RETRIES = 5


class _NullTicket:
    """Stand-in when no gate is wired (unit tests, one-off scripts)."""

    def release(self) -> None:
        pass

    def __enter__(self) -> _NullTicket:
        return self

    def __exit__(self, *exc_info: object) -> None:
        pass


def admit(
    gate: MediaJobGate | None, user_id: int, *, expected_bytes: int, heavy: bool
) -> JobTicket | _NullTicket:
    """Ask the gate for a slot; raises ServerBusy when there is none."""
    if gate is None:
        return _NullTicket()
    return gate.acquire(user_id, expected_bytes=expected_bytes, heavy=heavy)


async def commit_early(session: Any) -> None:
    """Persist the job row now and return the connection to the pool.

    The middleware commits again when the handler returns, which is harmless;
    what matters is that a 20-minute encode does not sit "idle in transaction".
    """
    if session is None:
        return
    await session.commit()


async def send_media(bot: Any, method: Any, *, size_bytes: int) -> Any:
    """Send an upload with a size-aware timeout, riding out flood waits.

    A split video sends many circles in a row, which is exactly when Telegram
    starts answering 429; waiting it out beats failing half-way through.
    """
    timeout = transfer_timeout(size_bytes)
    for attempt in range(_MAX_FLOOD_RETRIES + 1):
        try:
            return await bot(method, request_timeout=timeout)
        except TelegramRetryAfter as exc:
            if attempt == _MAX_FLOOD_RETRIES:
                raise
            logger.info("flood wait %ss before resending", exc.retry_after)
            await asyncio.sleep(exc.retry_after)
    return None  # pragma: no cover - loop always returns or raises


async def delete_quietly(message: Any) -> None:
    """Remove a status message; a refusal must never fail the job."""
    if message is None:
        return
    try:
        await message.delete()
    except Exception:  # noqa: BLE001 - deletion is best effort
        pass


class ProgressReporter:
    """Edits a status message at 25/50/75 %, and never more often than every
    ``min_interval`` seconds - a quick job shows no progress at all.

    ``render`` turns a percentage into the tool's own wording.
    """

    MILESTONES = (25, 50, 75)

    def __init__(
        self,
        status: Any,
        *,
        render: Callable[[int], str],
        min_interval: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._status = status
        self._render = render
        self._min_interval = min_interval
        self._clock = clock
        self._last_update = clock()
        self._shown = 0

    async def __call__(self, fraction: float) -> None:
        reached = [m for m in self.MILESTONES if self._shown < m <= fraction * 100]
        if not reached or self._status is None:
            return
        now = self._clock()
        if now - self._last_update < self._min_interval:
            return
        self._shown, self._last_update = reached[-1], now
        try:
            await self._status.edit_text(self._render(self._shown))
        except Exception:  # noqa: BLE001 - progress is cosmetic
            pass
