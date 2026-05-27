from bot.database import DraftDatabase
from bot.handlers import _render_sources_inventory, is_valid_rss_input_url, normalize_telegram_channel_input
from bot.sources import collect_topics_with_diagnostics, discover_rss_feed_url, parse_custom_topic_feeds


class _Resp:
    def __init__(self, text: str, content_type: str = "text/html"):
        self.text = text
        self.headers = {"content-type": content_type}

    def raise_for_status(self):
        return None


def test_managed_sources_crud(tmp_path):
    db = DraftDatabase(str(tmp_path / "db.sqlite"))
    sid = db.create_managed_source("rss", "Test", "https://example.com/feed.xml", "custom")
    assert sid > 0
    same = db.create_managed_source("rss", "Test2", "https://example.com/feed.xml", "custom")
    assert same == sid
    items = db.list_managed_sources()
    assert len(items) == 1
    db.update_managed_source_enabled(sid, False)
    assert db.get_managed_source(sid)["enabled"] == 0
    db.delete_managed_source(sid)
    assert db.get_managed_source(sid) is None


def test_telegram_normalize():
    assert normalize_telegram_channel_input("@channel") == "channel"
    assert normalize_telegram_channel_input("https://t.me/channel") == "channel"
    assert normalize_telegram_channel_input("t.me/channel") == "channel"
    assert normalize_telegram_channel_input("https://telegram.me/channel") == "channel"
    assert normalize_telegram_channel_input("https://t.me/s/channel") == "channel"
    assert normalize_telegram_channel_input("https://t.me/channel/123") == "channel"
    assert normalize_telegram_channel_input("https://t.me/+secret") == ""
    assert normalize_telegram_channel_input("https://t.me/joinchat/secret") == ""
    assert normalize_telegram_channel_input("https://t.me/c/123/4") == ""


def test_managed_sources_db_normalization_duplicate_telegram_and_rss(tmp_path):
    db = DraftDatabase(str(tmp_path / "db.sqlite"))
    sid1 = db.create_managed_source("telegram", "OpenAI", "@OpenAI", "telegram")
    sid2 = db.create_managed_source("telegram", "OpenAI2", "openai", "telegram")
    assert sid1 == sid2
    rid1 = db.create_managed_source("rss", "Feed", "https://example.com/feed/", "custom")
    rid2 = db.create_managed_source("rss", "Feed2", "https://example.com/feed", "custom")
    assert rid1 == rid2


def test_rss_validation():
    assert is_valid_rss_input_url("https://vc.ru/ai")
    assert not is_valid_rss_input_url("ftp://x")


def test_discover_direct_rss(monkeypatch):
    xml = "<rss><channel><item><title>A</title><link>https://x/a</link></item></channel></rss>"
    monkeypatch.setattr("bot.sources.requests.get", lambda *a, **k: _Resp(xml, "application/rss+xml"))
    url, err = discover_rss_feed_url("https://example.com/feed.xml")
    assert url == "https://example.com/feed.xml"
    assert err == ""


def test_discover_from_html_alternate_and_relative(monkeypatch):
    html = '<html><head><link rel="alternate" type="application/rss+xml" href="/feed.xml"/></head></html>'
    xml = "<rss><channel><item><title>A</title><link>https://x/a</link></item></channel></rss>"

    def fake_get(url, **kwargs):
        if url == "https://example.com/ai":
            return _Resp(html, "text/html")
        if url == "https://example.com/feed.xml":
            return _Resp(xml, "application/rss+xml")
        return _Resp("<html></html>")

    monkeypatch.setattr("bot.sources.requests.get", fake_get)
    url, err = discover_rss_feed_url("https://example.com/ai")
    assert url == "https://example.com/feed.xml"
    assert err == ""


def test_discover_no_rss(monkeypatch):
    monkeypatch.setattr("bot.sources.requests.get", lambda *a, **k: _Resp("<html></html>", "text/html"))
    url, err = discover_rss_feed_url("https://example.com/no")
    assert url is None
    assert "Не нашёл RSS" in err


def test_disabled_source_not_collected(monkeypatch, tmp_path):
    db = DraftDatabase(str(tmp_path / "db.sqlite"))
    sid = db.create_managed_source("rss", "Test", "https://example.com/feed.xml", "custom")
    db.update_managed_source_enabled(sid, False)
    monkeypatch.setattr("bot.sources.requests.get", lambda *a, **k: _Resp("<rss><channel></channel></rss>", "application/rss+xml"))
    items, reports = collect_topics_with_diagnostics(db=db)
    assert all(r.url != "https://example.com/feed.xml" for r in reports)


def test_custom_topic_feeds_unchanged():
    feeds = parse_custom_topic_feeds("Name|custom|https://example.com/feed.xml")
    assert feeds == [("Name", "custom", "https://example.com/feed.xml")]


def test_builtin_disabled_source_is_skipped(monkeypatch):
    rss_url = "https://example.com/builtin.xml"
    monkeypatch.setattr("bot.sources.OFFICIAL_AI_RSS", [("Built-in A", rss_url)])
    monkeypatch.setattr("bot.sources.TECH_MEDIA_RSS", [])
    monkeypatch.setattr("bot.sources.RU_TECH_RSS", [])
    monkeypatch.setattr("bot.sources.TOOLS_RSS", [])
    monkeypatch.setattr("bot.sources.COMMUNITY_RSS", [])
    monkeypatch.setattr("bot.sources.VC_RU_AI_SOURCE", ("vc.ru AI", "https://vc.ru/ai", "ru_tech"))
    monkeypatch.setattr("bot.sources.BUILTIN_SOURCE_OVERRIDES", {"rss:https://example.com/builtin.xml": {"action": "disable", "reason": "404/invalid feed"}})
    monkeypatch.setattr("bot.sources.fetch_vc_ru_ai_topics", lambda *a, **k: ([], type("R", (), {"name": "vc.ru AI", "url": "https://vc.ru/ai", "source_group": "ru_tech", "status": "empty", "item_count": 0, "error": ""})()))
    monkeypatch.setattr("bot.sources._fetch_github_trending_ai", lambda: [])

    def fake_get(*args, **kwargs):
        raise AssertionError("Disabled built-in source must not be fetched")

    monkeypatch.setattr("bot.sources.requests.get", fake_get)
    items, reports = collect_topics_with_diagnostics()
    assert items == []
    report = next(r for r in reports if r.name == "Built-in A")
    assert report.status == "skipped"
    assert "404/invalid feed" in report.error


def test_enabled_builtin_source_still_fetches(monkeypatch):
    rss_url = "https://example.com/builtin.xml"
    monkeypatch.setattr("bot.sources.OFFICIAL_AI_RSS", [("Built-in A", rss_url)])
    monkeypatch.setattr("bot.sources.TECH_MEDIA_RSS", [])
    monkeypatch.setattr("bot.sources.RU_TECH_RSS", [])
    monkeypatch.setattr("bot.sources.TOOLS_RSS", [])
    monkeypatch.setattr("bot.sources.COMMUNITY_RSS", [])
    monkeypatch.setattr("bot.sources.BUILTIN_SOURCE_OVERRIDES", {})
    monkeypatch.setattr("bot.sources.fetch_vc_ru_ai_topics", lambda *a, **k: ([], type("R", (), {"name": "vc.ru AI", "url": "https://vc.ru/ai", "source_group": "ru_tech", "status": "empty", "item_count": 0, "error": ""})()))
    monkeypatch.setattr("bot.sources._fetch_github_trending_ai", lambda: [])
    monkeypatch.setattr("bot.sources.requests.get", lambda *a, **k: _Resp("<rss><channel><item><title>A</title><link>https://x/a</link></item></channel></rss>", "application/rss+xml"))
    items, reports = collect_topics_with_diagnostics()
    assert len(items) == 1
    assert any(r.name == "Built-in A" and r.status == "ok" for r in reports)


def test_inventory_shows_disabled_builtin_reason(monkeypatch, tmp_path):
    monkeypatch.setattr("bot.sources.BUILTIN_SOURCE_OVERRIDES", {"rss:https://openai.com/news/rss.xml": {"action": "disable", "reason": "broken xml"}})
    db = DraftDatabase(str(tmp_path / "db.sqlite"))
    settings = type("S", (), {"db_path": str(tmp_path / "db.sqlite"), "telegram_source_channels": []})()
    text = "\n".join(_render_sources_inventory(settings, db))
    assert "⛔ [rss/official_ai] OpenAI blog" in text
    assert "broken xml" in text
