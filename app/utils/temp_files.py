"""Per-job temporary workspaces.

Every processing job gets its own directory ``{TEMP_ROOT}/{job_id}/`` which is
removed in a ``finally`` block once the job finishes, successfully or not.
User supplied filenames are never used on disk - we only borrow a validated
extension and generate a random internal name.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

logger = logging.getLogger(__name__)

# Extensions we are willing to reproduce on disk. Anything else falls back to
# ``.bin`` so a crafted filename can never introduce a surprising extension.
_ALLOWED_EXTENSIONS = frozenset(
    {
        ".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tiff", ".dng",
        ".gif",
        ".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".3gp",
    }
)
# Audio sources (Voice Note only). Kept apart from the list above so a photo or
# video job can never end up with an audio extension on disk.
AUDIO_EXTENSIONS = frozenset(
    {
        ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac",
        ".wma", ".aif", ".aiff", ".amr", ".caf", ".mka", ".weba", ".wv", ".ac3",
    }
)
_EXTENSION_RE = re.compile(r"^\.[A-Za-z0-9]{1,8}$")
_FALLBACK_EXTENSION = ".bin"


def safe_extension(
    original_filename: str | None,
    default: str = _FALLBACK_EXTENSION,
    *,
    allowed: frozenset[str] = _ALLOWED_EXTENSIONS,
) -> str:
    """Return a safe, lowercase extension derived from an untrusted filename."""
    if not original_filename:
        return default
    # ``PurePath`` on the raw string would still honour path separators, so we
    # deliberately look only at the trailing dot-segment of the basename.
    basename = original_filename.replace("\\", "/").rsplit("/", 1)[-1]
    if "." not in basename:
        return default
    ext = "." + basename.rsplit(".", 1)[-1].lower()
    if not _EXTENSION_RE.match(ext) or ext not in allowed:
        return default
    return ext


def safe_display_name(original_filename: str | None, fallback: str) -> str:
    """Sanitised name used only as the outgoing Telegram document filename."""
    if not original_filename:
        return fallback
    basename = original_filename.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", basename).strip("._")
    if not cleaned or cleaned in {".", ".."}:
        return fallback
    return cleaned[:64]


@dataclass(frozen=True)
class JobWorkspace:
    """An isolated directory owned by exactly one processing job."""

    job_id: uuid.UUID
    path: Path

    def new_file(self, extension: str = _FALLBACK_EXTENSION, prefix: str = "") -> Path:
        """Allocate a new random internal filename inside the workspace."""
        if not extension.startswith("."):
            extension = "." + extension
        name = f"{prefix}{uuid.uuid4().hex}{extension}"
        return self.path / name

    def usage_bytes(self) -> int:
        """Bytes currently on disk in this workspace, for per-job accounting."""
        total = 0
        for entry in self.path.rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:
                continue
        return total


# Batch workspaces are named "batch_<uuid>" so a directory listing says what
# they are; the sweeper below still recognises them.
BATCH_PREFIX = "batch_"


def _is_workspace_name(name: str) -> bool:
    candidate = name[len(BATCH_PREFIX):] if name.startswith(BATCH_PREFIX) else name
    try:
        uuid.UUID(candidate)
    except ValueError:
        return False
    return True


def sweep_stale_workspaces(root: Path) -> int:
    """Remove workspaces a previous process left behind.

    Run once at startup, before any job exists: a hard kill (deploy, OOM)
    skips the ``finally`` that normally deletes a workspace, and large media
    left there would quietly eat the ephemeral disk. Only the UUID-named
    directories :func:`job_workspace` creates (with or without the batch
    prefix) are touched.
    """
    root = Path(root)
    if not root.is_dir():
        return 0
    removed = 0
    for entry in root.iterdir():
        if entry.is_dir() and _is_workspace_name(entry.name):
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    if removed:
        logger.info("removed %d stale workspace(s) from %s", removed, root)
    return removed


@asynccontextmanager
async def job_workspace(
    root: Path, job_id: uuid.UUID | None = None, *, prefix: str = ""
) -> AsyncIterator[JobWorkspace]:
    """Create ``{root}/{prefix}{job_id}`` and always remove it on exit."""
    job_id = job_id or uuid.uuid4()
    path = Path(root) / f"{prefix}{job_id}"
    await asyncio.to_thread(path.mkdir, parents=True, exist_ok=True)
    try:
        yield JobWorkspace(job_id=job_id, path=path)
    finally:
        await asyncio.to_thread(shutil.rmtree, path, True)
        logger.debug("workspace removed", extra={"job_id": str(job_id)})
