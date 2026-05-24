from bot.database import DraftDatabase
from bot.handlers import is_valid_rss_input_url, normalize_telegram_channel_input
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
