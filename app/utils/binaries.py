"""Locating native helper binaries.

``PATH`` is the normal answer. Windows installers, however, only add their
directory to the *user* ``PATH``, which existing processes never pick up, so we
also look in the well-known install locations before giving up. Resolution is
cached because it only touches the filesystem.
"""

from __future__ import annotations

import logging
import os
import shutil
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)


def _windows_candidates(name: str) -> list[Path]:
    """Default per-user and machine-wide install directories on Windows."""
    roots = [
        os.environ.get("LOCALAPPDATA", ""),
        os.environ.get("ProgramFiles", ""),
        os.environ.get("ProgramFiles(x86)", ""),
    ]
    subdirs = [f"Programs/{name}", name]
    candidates: list[Path] = []
    for root in filter(None, roots):
        for subdir in subdirs:
            directory = Path(root) / subdir
            candidates.append(directory / f"{name}.exe")
    return candidates


@lru_cache(maxsize=8)
def resolve_binary(configured: str) -> str:
    """Return a runnable path for ``configured``, or the name unchanged.

    Returning the bare name when nothing is found is deliberate: the caller
    then fails with the usual "not installed or not on PATH" error instead of a
    different one raised from here.
    """
    # An explicit path in the settings always wins.
    candidate = Path(configured)
    if candidate.is_absolute():
        return str(candidate) if candidate.exists() else configured

    found = shutil.which(configured)
    if found:
        return found

    if os.name == "nt":
        for path in _windows_candidates(configured):
            if path.exists():
                logger.info("resolved %s outside PATH: %s", configured, path)
                return str(path)

    return configured


def is_available(configured: str) -> bool:
    """Whether the binary can actually be executed."""
    resolved = resolve_binary(configured)
    return Path(resolved).exists() or shutil.which(resolved) is not None
