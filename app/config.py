"""Typed application settings.

All runtime knobs live here so nothing else in the codebase reads os.environ.
"""

from __future__ import annotations

import logging
import tempfile
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.utils.binaries import resolve_binary

_VALID_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}

# Used whenever DATABASE_URL is absent: local dev needs no database server.
LOCAL_SQLITE_URL = "sqlite+aiosqlite:///./atreox_tools.db"


class Settings(BaseSettings):
    """Application configuration, populated from the environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Telegram ---------------------------------------------------------
    bot_token: str
    # Configurable so we can move to a self-hosted local Bot API server
    # without touching application code.
    bot_api_base_url: str = "https://api.telegram.org"

    # --- Database ---------------------------------------------------------
    # Local development default: a zero-dependency SQLite file next to the
    # project. Production (docker-compose, deploy) sets DATABASE_URL to the
    # PostgreSQL/asyncpg URL, which is still fully supported.
    database_url: str = LOCAL_SQLITE_URL
    db_echo: bool = False

    # --- Runtime ----------------------------------------------------------
    log_level: str = "INFO"
    max_file_size_mb: int = Field(default=20, ge=1, le=2000)
    process_timeout_seconds: int = Field(default=120, ge=5, le=3600)
    # Resolves to /tmp/atreox-tools in the container and to the user's temp
    # directory on Windows, so no drive-root write is ever attempted.
    temp_root: Path = Path(tempfile.gettempdir()) / "atreox-tools"

    # --- Native tools -----------------------------------------------------
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    exiftool_bin: str = "exiftool"

    # Comma separated Telegram user ids allowed to use dev/seed helpers.
    admin_user_ids: str = ""

    # --- Feature tuning ---------------------------------------------------
    stickers_per_batch: int = Field(default=6, ge=1, le=20)
    # Telegram refuses video notes longer than 60 seconds.
    video_note_max_duration: int = Field(default=60, ge=1, le=60)
    # Square side of the produced circle, must stay even for yuv420p.
    video_note_size: int = Field(default=384, ge=128, le=640)

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: object) -> object:
        if isinstance(value, str):
            upper = value.strip().upper()
            if upper not in _VALID_LOG_LEVELS:
                raise ValueError(
                    f"LOG_LEVEL must be one of {sorted(_VALID_LOG_LEVELS)}, got {value!r}"
                )
            return upper
        return value

    @field_validator("bot_api_base_url")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("BOT_API_BASE_URL must start with http:// or https://")
        return value

    @field_validator("video_note_size")
    @classmethod
    def _validate_even_size(cls, value: int) -> int:
        if value % 2:
            raise ValueError("VIDEO_NOTE_SIZE must be even (yuv420p requirement)")
        return value

    @property
    def admin_ids(self) -> frozenset[int]:
        ids = set()
        for chunk in self.admin_user_ids.replace(";", ",").split(","):
            chunk = chunk.strip()
            if chunk.lstrip("-").isdigit():
                ids.add(int(chunk))
        return frozenset(ids)

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    @property
    def log_level_int(self) -> int:
        return logging.getLevelName(self.log_level)

    @property
    def uses_local_bot_api(self) -> bool:
        return "api.telegram.org" not in self.bot_api_base_url

    @property
    def uses_sqlite(self) -> bool:
        """True for the local dev database; PostgreSQL stays the prod path."""
        return self.database_url.startswith("sqlite")

    @property
    def exiftool_path(self) -> str:
        """ExifTool as an actually runnable path.

        Windows installers only extend the *user* PATH, which a running process
        never sees, so this falls back to the standard install locations.
        """
        return resolve_binary(self.exiftool_bin)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor used by the application entrypoint."""
    return Settings()  # type: ignore[call-arg]
