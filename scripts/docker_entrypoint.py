"""Container entrypoint: prepare the database, then hand over to the bot.

Used as the image's ENTRYPOINT, with the actual process as its arguments:

    ENTRYPOINT ["python", "-m", "scripts.docker_entrypoint"]
    CMD ["python", "-m", "app.main"]

so ``docker run <image>`` boots the bot, while ``docker run <image> python -m
scripts.sync_stickers`` runs a one-off task against an already migrated
database. The final ``execvp`` replaces this process, so the bot keeps PID 1
and still receives the platform's stop signals.

Set ``SKIP_MIGRATIONS=1`` to bypass the schema step for a task that only needs
to read.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.logging_config import setup_logging  # noqa: E402
from app.startup import describe_database, prepare_database, wait_for_database  # noqa: E402

logger = logging.getLogger("entrypoint")


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level_int)

    logger.info("application starting")
    logger.info("database target: %s", describe_database(settings.database_url))

    if _truthy(os.environ.get("SKIP_MIGRATIONS")):
        logger.info("skipping migrations (SKIP_MIGRATIONS is set)")
        asyncio.run(wait_for_database(settings.database_url))
    else:
        asyncio.run(prepare_database(settings.database_url))

    command = sys.argv[1:] or ["python", "-m", "app.main"]
    logger.info("starting process: %s", " ".join(command))
    # Replace this process so signals reach the bot directly.
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
