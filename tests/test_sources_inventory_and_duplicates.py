import asyncio
from types import SimpleNamespace

from bot import handlers


def test_normalize_source_url_trailing_slash_and_host():
    assert handlers.normalize_source_url("HTTPS://Example.COM/path/") == "https://example.com/path"


def test_find_duplicate_rss_in_built_in(monkeypatch):
    settings = SimpleNamespace(telegram_source_channels=[], db_path="data/drafts.db")

    class _DB:
        def list_managed_sources(self, include_disabled=True):
            return []

    duplicate = handlers.find_duplicate_source("rss", "https://openai.com/news/rss.xml/", settings, _DB())
    assert duplicate is not None
    assert duplicate["location"] == "built-in"


def test_find_duplicate_rss_in_custom_topic_feeds(monkeypatch):
    monkeypatch.setenv("CUSTOM_TOPIC_FEEDS", "Demo|custom|https://example.com/feed/")
    settings = SimpleNamespace(telegram_source_channels=[], db_path="data/drafts.db")

    class _DB:
        def list_managed_sources(self, include_disabled=True):
            return []

    duplicate = handlers.find_duplicate_source("rss", "https://EXAMPLE.com/feed", settings, _DB())
    assert duplicate is not None
    assert duplicate["location"] == "env"


def test_find_duplicate_rss_in_managed_sources():
    settings = SimpleNamespace(telegram_source_channels=[], db_path="data/drafts.db")

    class _DB:
        def list_managed_sources(self, include_disabled=True):
            return [
                {
                    "source_type": "rss",
                    "source_group": "custom",
                    "name": "My Feed",
                    "value": "https://my.example/rss/",
                    "enabled": 1,
                }
            ]

    duplicate = handlers.find_duplicate_source("rss", "https://my.example/rss", settings, _DB())
    assert duplicate is not None
    assert duplicate["location"] == "my sources"


def test_find_duplicate_telegram_in_env_channels():
    settings = SimpleNamespace(telegram_source_channels=["@OpenAI"], db_path="data/drafts.db")

    class _DB:
        def list_managed_sources(self, include_disabled=True):
            return []

    duplicate = handlers.find_duplicate_source("telegram", "https://t.me/openai", settings, _DB())
    assert duplicate is not None
    assert duplicate["location"] == "env"


def test_inventory_renderer_does_not_expose_session_or_hash(monkeypatch):
    monkeypatch.setenv("CUSTOM_TOPIC_FEEDS", "Demo|custom|https://example.com/feed")
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", "SUPERSECRET")
    monkeypatch.setenv("TELEGRAM_API_HASH", "HASHSECRET")
    settings = SimpleNamespace(telegram_source_channels=["@openai"], db_path="data/drafts.db")

    class _DB:
        def list_managed_sources(self, include_disabled=True):
            return []

    text = "\n".join(handlers._render_sources_inventory(settings, _DB()))
    assert "SUPERSECRET" not in text
    assert "HASHSECRET" not in text


def test_sources_inventory_callback_exists_and_sends_list(monkeypatch):
    calls = []

    async def _fake_edit(_query, text, reply_markup=None):
        calls.append(text)

    monkeypatch.setattr(handlers, "_edit_callback_message", _fake_edit)

    class _DB:
        def list_managed_sources(self, include_disabled=True):
            return []

    class _Bot:
        async def send_message(self, chat_id, text, reply_markup=None):
            calls.append(text)

    context = SimpleNamespace(
        bot_data={"settings": SimpleNamespace(admin_id=1, telegram_source_channels=[], db_path="data/drafts.db"), "db": _DB()},
        bot=_Bot(),
        application=SimpleNamespace(bot_data={}),
        user_data={},
    )
    update = SimpleNamespace(callback_query=SimpleNamespace(data="sources_inventory", message=None))

    asyncio.run(handlers._handle_sources_callback(update, context, "sources_inventory"))

    assert any("Всего источников:" in call for call in calls)
