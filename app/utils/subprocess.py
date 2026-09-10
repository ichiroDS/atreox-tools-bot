"""Safe subprocess execution.

Commands are always passed as argument arrays - we never build a shell string
out of user input, and no shell is spawned at all.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Sequence

logger = logging.getLogger(__name__)

_MAX_CAPTURED_OUTPUT = 4000


class CommandError(RuntimeError):
    """Base class for subprocess failures."""


class CommandTimeout(CommandError):
    def __init__(self, program: str, timeout: float) -> None:
        super().__init__(f"{program} timed out after {timeout}s")
        self.program = program
        self.timeout = timeout


class CommandFailed(CommandError):
    def __init__(self, program: str, returncode: int, stderr: str) -> None:
        super().__init__(f"{program} exited with code {returncode}")
        self.program = program
        self.returncode = returncode
        self.stderr = stderr


class CommandNotFound(CommandError):
    def __init__(self, program: str) -> None:
        super().__init__(f"{program} is not installed or not on PATH")
        self.program = program


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


async def run_command(
    args: Sequence[str],
    *,
    timeout: float,
    job_id: str | None = None,
) -> CommandResult:
    """Run ``args`` without a shell, enforcing ``timeout`` seconds.

    Raises ``CommandTimeout``/``CommandFailed``/``CommandNotFound``. Only the
    program name and exit status are logged - never the media payload.
    """
    if not args:
        raise ValueError("run_command requires a non-empty argument array")

    argv = [str(a) for a in args]
    program = argv[0]
    log_extra = {"job_id": job_id or "-"}
    logger.debug("running %s (%d args)", program, len(argv) - 1, extra=log_extra)

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:  # pragma: no cover - environment dependent
        raise CommandNotFound(program) from exc

    try:
        stdout_b, stderr_b = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        _terminate(process)
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:  # pragma: no cover - defensive
            pass
        logger.warning("%s timed out after %ss", program, timeout, extra=log_extra)
        raise CommandTimeout(program, timeout) from None

    stdout = stdout_b.decode("utf-8", "replace")
    stderr = stderr_b.decode("utf-8", "replace")[:_MAX_CAPTURED_OUTPUT]

    if process.returncode != 0:
        logger.warning(
            "%s failed rc=%s: %s", program, process.returncode, stderr.strip()[:500],
            extra=log_extra,
        )
        raise CommandFailed(program, process.returncode or -1, stderr)

    return CommandResult(returncode=0, stdout=stdout, stderr=stderr)


def _terminate(process: asyncio.subprocess.Process) -> None:
    try:
        process.kill()
    except ProcessLookupError:  # pragma: no cover - race with natural exit
        pass
