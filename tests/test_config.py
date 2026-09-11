from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


MB = 1024 * 1024


def test_defaults(clean_env):
    settings = Settings(bot_token="123:abc")
    assert settings.bot_api_base_url == "https://api.telegram.org"
    assert settings.uses_local_bot_api is False
    assert settings.bot_api_files_url is None
    assert settings.max_input_file_size_mb == 2000
    assert settings.max_output_file_size_mb == 1950
    assert settings.max_concurrent_media_jobs == 2
    assert settings.process_timeout_seconds == 600
    assert settings.log_level == "INFO"


def test_reads_environment(clean_env):
    clean_env.setenv("BOT_TOKEN", "999:xyz")
    clean_env.setenv("LOG_LEVEL", "debug")
    clean_env.setenv("BOT_API_BASE_URL", "http://bot-api:8081")
    clean_env.setenv("BOT_API_FILES_URL", "http://bot-api:8082/")
    clean_env.setenv("MAX_INPUT_FILE_SIZE_MB", "500")
    clean_env.setenv("MAX_OUTPUT_FILE_SIZE_MB", "400")
    clean_env.setenv("MAX_CONCURRENT_MEDIA_JOBS", "3")
    clean_env.setenv("PROCESS_TIMEOUT_SECONDS", "900")
    settings = Settings()
    assert settings.bot_token == "999:xyz"
    assert settings.log_level == "DEBUG"
    assert settings.bot_api_files_url == "http://bot-api:8082"
    assert settings.input_limit_bytes == 500 * MB
    assert settings.output_limit_bytes == 400 * MB
    assert settings.max_concurrent_media_jobs == 3
    assert settings.process_timeout_seconds == 900


def test_cloud_api_caps_the_configured_limits(clean_env):
    # Until the local server is live, Telegram's own ceilings still apply, so
    # the bot never accepts a file the cloud API cannot hand over.
    settings = Settings(bot_token="1:a")
    assert settings.input_limit_bytes == 20 * MB
    assert settings.output_limit_bytes == 50 * MB


def test_local_api_lifts_the_caps_to_the_configured_limits(clean_env):
    settings = Settings(bot_token="1:a", bot_api_base_url="http://bot-api:8081")
    assert settings.input_limit_bytes == 2000 * MB
    assert settings.output_limit_bytes == 1950 * MB


def test_output_limit_never_exceeds_the_local_upload_ceiling(clean_env):
    settings = Settings(
        bot_token="1:a", bot_api_base_url="http://bot-api:8081", max_output_file_size_mb=2000
    )
    assert settings.output_limit_bytes == 2000 * MB
    with pytest.raises(ValidationError):
        Settings(bot_token="1:a", max_output_file_size_mb=2001)


def test_blank_files_url_means_unset(clean_env):
    assert Settings(bot_token="1:a", bot_api_files_url="  ").bot_api_files_url is None


def test_rejects_non_http_files_url(clean_env):
    with pytest.raises(ValidationError):
        Settings(bot_token="1:a", bot_api_files_url="file:///var/lib")


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
