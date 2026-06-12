import pytest

from bot.config import load_settings, startup_diagnostics


_REQUIRED_ENV = {
    "BOT_TOKEN": "test-token",
    "ADMIN_ID": "123456",
    "CHANNEL_ID": "@simplify_ai",
}

_OPTIONAL_ENV_NAMES = [
    "STRICT_CONFIG",
    "POST_MAX_CHARS",
    "POST_SOFT_CHARS",
    "MAX_TOPIC_AGE_DAYS",
    "TOPIC_AI_ENRICH_LIMIT",
    "TOPIC_AI_TRANSLATE_LIMIT",
    "X_MAX_POSTS_PER_ACCOUNT",
    "TELEGRAM_SOURCE_LOOKBACK_HOURS",
    "TELEGRAM_SOURCE_MAX_POSTS_PER_CHANNEL",
    "DAILY_POST_SLOTS",
    "SCHEDULE_TIMEZONE",
    "TELEGRAM_API_ID",
    "ENABLE_X_SOURCES",
    "X_API_BEARER_TOKEN",
    "X_ACCOUNTS",
    "ENABLE_TELEGRAM_CHANNEL_SOURCES",
    "TELEGRAM_API_HASH",
    "TELEGRAM_SESSION_STRING",
    "TELEGRAM_SOURCE_CHANNELS",
    "CUSTOM_EMOJI_MAP",
    "CUSTOM_EMOJI_ALIASES",
    "DB_PATH",
    "RAILWAY_ENVIRONMENT",
    "RAILWAY_PROJECT_ID",
    "RAILWAY_SERVICE_ID",
]


def _clean_config_env(monkeypatch):
    for name in _OPTIONAL_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)


def test_invalid_numeric_env_falls_back_with_warning_in_non_strict_mode(monkeypatch):
    _clean_config_env(monkeypatch)
    monkeypatch.setenv("MAX_TOPIC_AGE_DAYS", "not-a-number")

    settings = load_settings()

    assert settings.max_topic_age_days == 14
    assert any("MAX_TOPIC_AGE_DAYS" in warning for warning in settings.config_warnings)
    assert any("CONFIG WARNING: MAX_TOPIC_AGE_DAYS" in line for line in startup_diagnostics(settings))


def test_invalid_numeric_env_raises_in_strict_mode(monkeypatch):
    _clean_config_env(monkeypatch)
    monkeypatch.setenv("STRICT_CONFIG", "1")
    monkeypatch.setenv("MAX_TOPIC_AGE_DAYS", "not-a-number")

    with pytest.raises(ValueError, match="MAX_TOPIC_AGE_DAYS"):
        load_settings()


def test_invalid_daily_post_slots_warns_and_falls_back(monkeypatch):
    _clean_config_env(monkeypatch)
    monkeypatch.setenv("DAILY_POST_SLOTS", "25:99,not-time")

    settings = load_settings()

    assert settings.daily_post_slots == ["10:00", "14:00", "18:00", "21:00"]
    assert any("DAILY_POST_SLOTS" in warning for warning in settings.config_warnings)


def test_duplicate_daily_post_slots_are_removed(monkeypatch):
    _clean_config_env(monkeypatch)
    monkeypatch.setenv("DAILY_POST_SLOTS", "10:00,10:00,14:00")

    settings = load_settings()

    assert settings.daily_post_slots == ["10:00", "14:00"]
    assert any("duplicate" in warning for warning in settings.config_warnings)


def test_invalid_timezone_falls_back_with_warning(monkeypatch):
    _clean_config_env(monkeypatch)
    monkeypatch.setenv("SCHEDULE_TIMEZONE", "Mars/Olympus")

    settings = load_settings()

    assert settings.schedule_timezone == "Europe/Moscow"
    assert any("SCHEDULE_TIMEZONE" in warning for warning in settings.config_warnings)


def test_invalid_timezone_raises_in_strict_mode(monkeypatch):
    _clean_config_env(monkeypatch)
    monkeypatch.setenv("STRICT_CONFIG", "1")
    monkeypatch.setenv("SCHEDULE_TIMEZONE", "Mars/Olympus")

    with pytest.raises(ValueError, match="SCHEDULE_TIMEZONE"):
        load_settings()


def test_enabled_x_sources_without_credentials_warns_in_non_strict_mode(monkeypatch):
    _clean_config_env(monkeypatch)
    monkeypatch.setenv("ENABLE_X_SOURCES", "true")

    settings = load_settings()

    assert settings.enable_x_sources is True
    assert any("ENABLE_X_SOURCES" in warning for warning in settings.config_warnings)
    assert any("X_API_BEARER_TOKEN" in warning and "X_ACCOUNTS" in warning for warning in settings.config_warnings)


def test_enabled_x_sources_without_credentials_raises_in_strict_mode(monkeypatch):
    _clean_config_env(monkeypatch)
    monkeypatch.setenv("STRICT_CONFIG", "yes")
    monkeypatch.setenv("ENABLE_X_SOURCES", "on")

    with pytest.raises(ValueError, match="ENABLE_X_SOURCES"):
        load_settings()


def test_enabled_telegram_channel_sources_without_credentials_warns_in_non_strict_mode(monkeypatch):
    _clean_config_env(monkeypatch)
    monkeypatch.setenv("ENABLE_TELEGRAM_CHANNEL_SOURCES", "true")

    settings = load_settings()

    assert settings.enable_telegram_channel_sources is True
    assert any("ENABLE_TELEGRAM_CHANNEL_SOURCES" in warning for warning in settings.config_warnings)
    assert any("TELEGRAM_API_ID" in warning for warning in settings.config_warnings)


def test_enabled_telegram_channel_sources_without_credentials_raises_in_strict_mode(monkeypatch):
    _clean_config_env(monkeypatch)
    monkeypatch.setenv("STRICT_CONFIG", "true")
    monkeypatch.setenv("ENABLE_TELEGRAM_CHANNEL_SOURCES", "true")

    with pytest.raises(ValueError, match="ENABLE_TELEGRAM_CHANNEL_SOURCES"):
        load_settings()


def test_railway_default_db_path_warns_in_non_strict_mode(monkeypatch):
    _clean_config_env(monkeypatch)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setenv("DB_PATH", "data/drafts.db")

    settings = load_settings()

    assert any(
        "Railway" in warning and "DB_PATH" in warning and "/data/drafts.db" in warning
        for warning in settings.config_warnings
    )
    assert any(
        "CONFIG WARNING:" in line and "DB_PATH" in line
        for line in startup_diagnostics(settings)
    )


def test_railway_default_db_path_raises_in_strict_mode(monkeypatch):
    _clean_config_env(monkeypatch)
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "project-id")
    monkeypatch.setenv("DB_PATH", "./data/drafts.db")
    monkeypatch.setenv("STRICT_CONFIG", "1")

    with pytest.raises(ValueError, match="DB_PATH.*Railway"):
        load_settings()


def test_railway_custom_db_path_does_not_warn_or_fail(monkeypatch, tmp_path):
    _clean_config_env(monkeypatch)
    custom_db_path = tmp_path / "volume" / "drafts.db"
    monkeypatch.setenv("RAILWAY_SERVICE_ID", "service-id")
    monkeypatch.setenv("DB_PATH", str(custom_db_path))
    monkeypatch.setenv("STRICT_CONFIG", "1")

    settings = load_settings()

    assert settings.db_path == str(custom_db_path)
    assert not any("persistent volume" in warning for warning in settings.config_warnings)
    assert custom_db_path.parent.is_dir()


def test_db_path_parent_validation_fails_when_parent_is_file(monkeypatch, tmp_path):
    _clean_config_env(monkeypatch)
    file_parent = tmp_path / "not-a-directory"
    file_parent.write_text("already a file", encoding="utf-8")
    monkeypatch.setenv("DB_PATH", str(file_parent / "drafts.db"))

    with pytest.raises(ValueError, match="DB_PATH parent"):
        load_settings()


def test_custom_emoji_configuration_counts_and_warns_about_malformed_entries(monkeypatch):
    _clean_config_env(monkeypatch)
    monkeypatch.setenv("CUSTOM_EMOJI_MAP", "🔥|111;not-emoji|222;💭|bad-id")
    monkeypatch.setenv("CUSTOM_EMOJI_ALIASES", "fire|🔥|111;bad|text|222;broken")

    settings = load_settings()
    diagnostics = startup_diagnostics(settings)

    assert settings.custom_emoji_map == {"🔥": "111"}
    assert settings.custom_emoji_aliases == {"fire": ("🔥", "111")}
    assert "custom emoji map count: 1" in diagnostics
    assert "custom emoji aliases count: 1" in diagnostics
    assert any("CUSTOM_EMOJI_MAP has 2 malformed entries" in line for line in diagnostics)
    assert any("CUSTOM_EMOJI_ALIASES has 2 malformed entries" in line for line in diagnostics)
