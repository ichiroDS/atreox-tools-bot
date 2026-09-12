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
from sqlalchemy.engine import make_url

from app.utils.binaries import resolve_binary

logger = logging.getLogger(__name__)

_VALID_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}

# Used whenever DATABASE_URL is absent: local dev needs no database server.
LOCAL_SQLITE_URL = "sqlite+aiosqlite:///./atreox_tools.db"

OFFICIAL_BOT_API_URL = "https://api.telegram.org"

_MB = 1024 * 1024

# Transport ceilings imposed by Telegram itself. The configured limits below
# are service safety limits; the effective limit is whichever is lower, so the
# bot never accepts work the current Bot API server cannot carry.
CLOUD_DOWNLOAD_LIMIT_BYTES = 20 * _MB
CLOUD_UPLOAD_LIMIT_BYTES = 50 * _MB
LOCAL_UPLOAD_LIMIT_BYTES = 2000 * _MB

# The only async drivers this application ships. Everything the engine touches
# goes through create_async_engine, so a synchronous driver cannot work here.
ASYNC_DRIVERS: dict[str, str] = {
    # "postgres" is the legacy scheme Heroku-style platforms still hand out.
    "postgres": "postgresql+asyncpg",
    "postgresql": "postgresql+asyncpg",
    "sqlite": "sqlite+aiosqlite",
}


def normalise_database_url(raw: str) -> str:
    """Force a DATABASE_URL onto this application's async driver.

    Managed platforms hand out libpq-style URLs - Railway and Heroku supply
    ``postgres://`` or ``postgresql://``. SQLAlchemy reads those as "use the
    default driver", which is psycopg2, and the app dies at import with
    ``No module named 'psycopg2'``. Rewriting the scheme here means every
    consumer - the bot, the entrypoint and Alembic - agrees on asyncpg, and no
    synchronous driver ever has to be installed.

    A URL that already names its async driver is returned untouched.
    """
    raw = (raw or "").strip()
    if not raw:
        return LOCAL_SQLITE_URL

    url = make_url(raw)
    backend = url.drivername.split("+", 1)[0]
    target = ASYNC_DRIVERS.get(backend)
    if target is None:
        # An unknown backend is the operator's business, not ours to rewrite.
        return raw

    if url.drivername != target:
        logger.info(
            "normalised DATABASE_URL driver %r -> %r", url.drivername, target
        )
        url = url.set(drivername=target)

    # libpq spells it "sslmode"; asyncpg only understands "ssl", and would
    # otherwise fail at connect time with an unexpected keyword argument.
    query = dict(url.query)
    sslmode = query.pop("sslmode", None)
    if sslmode is not None:
        query.setdefault("ssl", sslmode)
        url = url.set(query=query)

    return url.render_as_string(hide_password=False)


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
    bot_api_base_url: str = OFFICIAL_BOT_API_URL
    # Local mode only. A local Bot API server answers getFile with an absolute
    # path on *its* disk. When the bot runs in another container (a separate
    # Railway service) that path is fetched from this HTTP file server instead,
    # e.g. http://telegram-bot-api.railway.internal:8082.
    bot_api_files_url: str | None = None
    # The server's --dir, used to turn its absolute paths into file-server URLs.
    bot_api_local_dir: str = "/var/lib/telegram-bot-api"

    # --- Database ---------------------------------------------------------
    # Local development default: a zero-dependency SQLite file next to the
    # project. Production (docker-compose, deploy) sets DATABASE_URL to the
    # PostgreSQL/asyncpg URL, which is still fully supported.
    database_url: str = LOCAL_SQLITE_URL
    db_echo: bool = False

    # --- Runtime ----------------------------------------------------------
    log_level: str = "INFO"
    # Service safety limits, not Telegram promises: see input_limit_bytes and
    # output_limit_bytes for what is actually enforced.
    max_input_file_size_mb: int = Field(default=2000, ge=1, le=4000)
    max_output_file_size_mb: int = Field(default=1950, ge=1, le=2000)
    # Per native-tool invocation (one ffprobe, one exiftool run, one circle
    # segment encode), so a long split never has to fit in a single budget.
    process_timeout_seconds: int = Field(default=600, ge=5, le=7200)
    # Heavy jobs (large files, split videos) that may run at once. Small files
    # never wait on this, so everyday use is unaffected.
    max_concurrent_media_jobs: int = Field(default=2, ge=1, le=32)
    # Disk the job workspaces may claim in total. 0 = derive it from the free
    # space under TEMP_ROOT at the moment a job starts.
    temp_disk_budget_mb: int = Field(default=0, ge=0)
    # Resolves to /tmp/atreox-tools in the container and to the user's temp
    # directory on Windows, so no drive-root write is ever attempted.
    temp_root: Path = Path(tempfile.gettempdir()) / "atreox-tools"

    # --- Native tools -----------------------------------------------------
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    exiftool_bin: str = "exiftool"
    # TrueType font the watermark is drawn with. Empty means "find one": the
    # Docker image installs fonts-dejavu-core, and the known system fonts are
    # tried in turn (see app/services/media/watermark.py).
    watermark_font: str | None = None

    # Comma separated Telegram user ids allowed to use dev/seed helpers.
    admin_user_ids: str = ""

    # --- Feature tuning ---------------------------------------------------
    stickers_per_batch: int = Field(default=6, ge=1, le=20)
    # Telegram refuses video notes longer than 60 seconds.
    video_note_max_duration: int = Field(default=60, ge=1, le=60)
    # Square side of the produced circle, must stay even for yuv420p.
    video_note_size: int = Field(default=384, ge=128, le=640)

    @field_validator("database_url", mode="before")
    @classmethod
    def _normalise_database_url(cls, value: object) -> object:
        if isinstance(value, str):
            return normalise_database_url(value)
        return value

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

    @field_validator("bot_api_files_url", mode="before")
    @classmethod
    def _validate_files_url(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            value = value.strip().rstrip("/")
            if not value:
                return None
            if not value.startswith(("http://", "https://")):
                raise ValueError("BOT_API_FILES_URL must start with http:// or https://")
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
    def input_limit_bytes(self) -> int:
        """Largest upload the bot will accept.

        The cloud Bot API cannot hand over more than 20 MB whatever we
        configure, so until the local server is in use that stays the ceiling.
        """
        configured = self.max_input_file_size_mb * _MB
        if self.uses_local_bot_api:
            return configured
        return min(configured, CLOUD_DOWNLOAD_LIMIT_BYTES)

    @property
    def output_limit_bytes(self) -> int:
        """Largest file the bot will try to send back."""
        ceiling = (
            LOCAL_UPLOAD_LIMIT_BYTES if self.uses_local_bot_api else CLOUD_UPLOAD_LIMIT_BYTES
        )
        return min(self.max_output_file_size_mb * _MB, ceiling)

    @property
    def temp_disk_budget_bytes(self) -> int:
        return self.temp_disk_budget_mb * _MB

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
