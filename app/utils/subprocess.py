"""Safe subprocess execution.

Commands are always passed as argument arrays - we never build a shell string
out of user input, and no shell is spawned at all.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Sequence

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


async def run_command_streaming(
    args: Sequence[str],
    *,
    timeout: float,
    on_line: Callable[[str], Awaitable[None]],
    job_id: str | None = None,
) -> CommandResult:
    """Like :func:`run_command`, but hands each stdout line to ``on_line`` as it
    arrives - for tools that report progress (``ffmpeg -progress pipe:1``).

    stderr is drained concurrently, keeping only its tail, so a chatty tool can
    never block on a full pipe. ``on_line`` failures are logged and ignored:
    progress is cosmetic and must not fail the job.
    """
    if not args:
        raise ValueError("run_command_streaming requires a non-empty argument array")

    argv = [str(a) for a in args]
    program = argv[0]
    log_extra = {"job_id": job_id or "-"}
    logger.debug("running %s (%d args, streaming)", program, len(argv) - 1, extra=log_extra)

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:  # pragma: no cover - environment dependent
        raise CommandNotFound(program) from exc

    stderr_tail = bytearray()

    async def drain_stdout() -> None:
        assert process.stdout is not None
        async for raw in process.stdout:
            try:
                await on_line(raw.decode("utf-8", "replace").strip())
            except Exception:  # noqa: BLE001 - progress must never fail the job
                logger.debug("progress callback failed", exc_info=True, extra=log_extra)

    async def drain_stderr() -> None:
        assert process.stderr is not None
        while chunk := await process.stderr.read(4096):
            stderr_tail.extend(chunk)
            del stderr_tail[:-_MAX_CAPTURED_OUTPUT]

    async def run() -> None:
        await asyncio.gather(drain_stdout(), drain_stderr())
        await process.wait()

    try:
        await asyncio.wait_for(run(), timeout=timeout)
    except asyncio.TimeoutError:
        _terminate(process)
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:  # pragma: no cover - defensive
            pass
        logger.warning("%s timed out after %ss", program, timeout, extra=log_extra)
        raise CommandTimeout(program, timeout) from None

    stderr = stderr_tail.decode("utf-8", "replace")
    if process.returncode != 0:
        logger.warning(
            "%s failed rc=%s: %s", program, process.returncode, stderr.strip()[:500],
            extra=log_extra,
        )
        raise CommandFailed(program, process.returncode or -1, stderr)
    return CommandResult(returncode=0, stdout="", stderr=stderr)


def _terminate(process: asyncio.subprocess.Process) -> None:
    try:
        process.kill()
    except ProcessLookupError:  # pragma: no cover - race with natural exit
        pass
