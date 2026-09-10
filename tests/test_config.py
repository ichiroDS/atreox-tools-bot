from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_defaults(clean_env):
    settings = Settings(bot_token="123:abc")
    assert settings.bot_api_base_url == "https://api.telegram.org"
    assert settings.uses_local_bot_api is False
    assert settings.max_file_size_bytes == 20 * 1024 * 1024
    assert settings.log_level == "INFO"


def test_reads_environment(clean_env):
    clean_env.setenv("BOT_TOKEN", "999:xyz")
    clean_env.setenv("LOG_LEVEL", "debug")
    clean_env.setenv("MAX_FILE_SIZE_MB", "50")
    settings = Settings()
    assert settings.bot_token == "999:xyz"
    assert settings.log_level == "DEBUG"
    assert settings.max_file_size_bytes == 50 * 1024 * 1024


def test_local_bot_api_detected(clean_env):
    settings = Settings(bot_token="1:a", bot_api_base_url="http://bot-api:8081/")
    assert settings.bot_api_base_url == "http://bot-api:8081"
    assert settings.uses_local_bot_api is True


def test_rejects_bad_log_level(clean_env):
    with pytest.raises(ValidationError):
        Settings(bot_token="1:a", log_level="chatty")


def test_rejects_non_http_api_url(clean_env):
    with pytest.raises(ValidationError):
        Settings(bot_token="1:a", bot_api_base_url="ftp://example.com")


def test_rejects_odd_video_note_size(clean_env):
    with pytest.raises(ValidationError):
        Settings(bot_token="1:a", video_note_size=385)


def test_missing_token_is_fatal(clean_env):
    with pytest.raises(ValidationError):
        Settings()


def test_admin_ids_parsing(clean_env):
    settings = Settings(bot_token="1:a", admin_user_ids=" 42, 7 ,notanid,")
    assert settings.admin_ids == frozenset({42, 7})


def test_admin_ids_empty_by_default(clean_env):
    assert Settings(bot_token="1:a").admin_ids == frozenset()
