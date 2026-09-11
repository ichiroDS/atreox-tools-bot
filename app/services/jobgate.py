"""Admission control for media jobs: CPU slots and disk accounting.

Every job asks the gate before it touches a byte. The answer is immediate -
nothing ever queues inside a Telegram handler - so an overloaded server says
"busy, try again shortly" instead of piling up work it cannot finish.

* **Heavy jobs** (large files, split videos) share ``max_heavy_jobs`` slots,
  and a user runs at most one of them at a time.
* **Light jobs** (what the bot always handled: small files) skip the slots,
  so everyday use never waits behind someone's 2 GB upload.
* **Every job** reserves the disk it expects to need. A reservation is only
  granted while the workspace filesystem keeps a safety margin free, so one
  large job cannot exhaust the disk another job is writing to.

State is in-process, which is the right scope: the bot is a single polling
process, and the workspaces it guards live on that process's disk.
"""

from __future__ import annotations

import enum
import logging
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

_MB = 1024 * 1024

# Anything above this is "heavy": it is what the cloud Bot API used to cap
# downloads at, so jobs this size are exactly the ones the bot always ran.
HEAVY_JOB_THRESHOLD_BYTES = 20 * _MB

# Never promise away the last of the disk - the OS, logs and ffmpeg's own
# scratch files need room too.
DISK_SAFETY_MARGIN_BYTES = 512 * _MB


class BusyReason(str, enum.Enum):
    SERVER = "server"  # every heavy slot is taken
    USER = "user"      # this user already has a heavy job running
    DISK = "disk"      # not enough free workspace disk for this job


class ServerBusy(Exception):
    def __init__(self, reason: BusyReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


def estimate_job_bytes(input_size: int | None, *, factor: float = 1.0, extra: int = 0) -> int:
    """Disk a job will need: ``factor`` copies of the input plus ``extra``."""
    return int((input_size or HEAVY_JOB_THRESHOLD_BYTES) * factor) + extra


@dataclass
class JobTicket:
    """Proof of admission. Release it (or use ``with``) when the job ends."""

    gate: MediaJobGate
    user_id: int
    reserved_bytes: int
    heavy: bool
    _released: bool = field(default=False, repr=False)

    def release(self) -> None:
        if not self._released:
            self._released = True
            self.gate._release(self)

    def __enter__(self) -> JobTicket:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def _free_bytes(path: Path) -> int:
    probe = Path(path)
    # The root may not exist yet on a fresh container; its parent is the same disk.
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


class MediaJobGate:
    def __init__(
        self,
        *,
        max_heavy_jobs: int,
        temp_root: Path,
        disk_budget_bytes: int = 0,
        heavy_threshold_bytes: int = HEAVY_JOB_THRESHOLD_BYTES,
        safety_margin_bytes: int = DISK_SAFETY_MARGIN_BYTES,
        free_bytes: Callable[[Path], int] = _free_bytes,
    ) -> None:
        self._max_heavy_jobs = max_heavy_jobs
        self._temp_root = Path(temp_root)
        self._disk_budget_bytes = disk_budget_bytes
        self._heavy_threshold_bytes = heavy_threshold_bytes
        self._safety_margin_bytes = safety_margin_bytes
        self._free_bytes = free_bytes
        self._lock = threading.Lock()
        self._heavy_running = 0
        self._heavy_users: set[int] = set()
        self._reserved_bytes = 0

    @classmethod
    def from_settings(cls, settings) -> MediaJobGate:
        return cls(
            max_heavy_jobs=settings.max_concurrent_media_jobs,
            temp_root=settings.temp_root,
            disk_budget_bytes=settings.temp_disk_budget_bytes,
        )

    @property
    def heavy_running(self) -> int:
        return self._heavy_running

    @property
    def reserved_bytes(self) -> int:
        return self._reserved_bytes

    def is_heavy(self, input_size: int | None) -> bool:
        return (input_size or 0) > self._heavy_threshold_bytes

    def acquire(self, user_id: int, *, expected_bytes: int, heavy: bool) -> JobTicket:
        """Admit a job now or raise :class:`ServerBusy` - never waits."""
        with self._lock:
            if heavy:
                if user_id in self._heavy_users:
                    raise self._refuse(BusyReason.USER, user_id)
                if self._heavy_running >= self._max_heavy_jobs:
                    raise self._refuse(BusyReason.SERVER, user_id)

            if not self._disk_allows(expected_bytes):
                raise self._refuse(BusyReason.DISK, user_id)

            self._reserved_bytes += expected_bytes
            if heavy:
                self._heavy_running += 1
                self._heavy_users.add(user_id)

        logger.info(
            "media job admitted telegram_user_id=%s heavy=%s reserved=%dMB "
            "(heavy running %d/%d, reserved total %dMB)",
            user_id,
            heavy,
            expected_bytes // _MB,
            self._heavy_running,
            self._max_heavy_jobs,
            self._reserved_bytes // _MB,
        )
        return JobTicket(self, user_id, expected_bytes, heavy)

    def _disk_allows(self, expected_bytes: int) -> bool:
        committed = self._reserved_bytes + expected_bytes
        if self._disk_budget_bytes and committed > self._disk_budget_bytes:
            return False
        try:
            free = self._free_bytes(self._temp_root)
        except OSError:
            # Unknown is not the same as full; the stream-time size limit and
            # per-job cleanup still protect us.
            return True
        # Reservations of running jobs may not be written yet, so they count
        # against today's free space too. Deliberately conservative.
        return free - self._safety_margin_bytes - self._reserved_bytes >= expected_bytes

    def _refuse(self, reason: BusyReason, user_id: int) -> ServerBusy:
        logger.info("media job refused reason=%s telegram_user_id=%s", reason.value, user_id)
        return ServerBusy(reason)

    def _release(self, ticket: JobTicket) -> None:
        with self._lock:
            self._reserved_bytes = max(0, self._reserved_bytes - ticket.reserved_bytes)
            if ticket.heavy:
                self._heavy_running = max(0, self._heavy_running - 1)
                self._heavy_users.discard(ticket.user_id)
