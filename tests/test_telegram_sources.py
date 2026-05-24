from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from bot.telegram_sources import _message_to_topic, fetch_telegram_channel_topics


def _settings(**kwargs):
    base = dict(
        enable_telegram_channel_sources=True,
        telegram_api_id=1,
        telegram_api_hash="hash",
        telegram_session_string="session",
        telegram_source_channels=["openai"],
        telegram_source_lookback_hours=24,
        telegram_source_max_posts_per_channel=20,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_message_to_topic_conversion_uses_description():
    now = datetime.now(timezone.utc)
    msg = SimpleNamespace(id=11, service=False, date=now, raw_text="AI workflow demo with useful links and practical creator tips " * 2)
    topic = _message_to_topic(msg, "openai", now - timedelta(hours=24))
    assert topic is not None
    assert topic.url == "https://t.me/openai/11"
    assert topic.source_group == "telegram"
    assert topic.original_description.startswith("AI workflow demo")
    assert topic.score > 0


def test_older_than_lookback_is_skipped():
    now = datetime.now(timezone.utc)
    msg = SimpleNamespace(id=12, service=False, date=now - timedelta(hours=30), raw_text="A" * 80)
    assert _message_to_topic(msg, "openai", now - timedelta(hours=24)) is None


def test_empty_or_short_skipped():
    now = datetime.now(timezone.utc)
    short = SimpleNamespace(id=13, service=False, date=now, raw_text="short text")
    empty = SimpleNamespace(id=14, service=False, date=now, raw_text="")
    assert _message_to_topic(short, "openai", now - timedelta(hours=24)) is None
    assert _message_to_topic(empty, "openai", now - timedelta(hours=24)) is None


def test_missing_config_returns_skipped():
    import asyncio
    items, reports = asyncio.run(fetch_telegram_channel_topics(_settings(telegram_api_id=None)))
    assert items == []
    assert reports[0].status == "skipped"
    assert "missing" in reports[0].error.lower()
