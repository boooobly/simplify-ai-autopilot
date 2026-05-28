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
