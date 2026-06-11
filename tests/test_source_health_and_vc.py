from pathlib import Path
from types import SimpleNamespace

import pytest

from bot.database import DraftDatabase
import bot.sources as sources
from bot.sources import SourceReport, collect_topics_with_diagnostics, fetch_vc_ru_ai_topics
from bot.source_candidates import CANDIDATE_SOURCES
from bot.handlers import _render_sources_health


def test_source_health_table_creation(tmp_path):
    db = DraftDatabase(str(tmp_path / 't.db'))
    rows = db.list_source_health()
    assert rows == []


def test_source_health_error_cooldown(tmp_path):
    db = DraftDatabase(str(tmp_path / 't.db'))
    for _ in range(3):
        db.record_source_health('rss', 'https://a', 'A', 'tech_media', 'error', '404')
    skip, _ = db.should_skip_source('rss', 'https://a')
    assert skip
    for _ in range(3):
        db.record_source_health('rss', 'https://a', 'A', 'tech_media', 'error', '404')
    row = db.get_source_health('rss', 'https://a')
    assert int(row['consecutive_errors']) >= 6


def test_source_health_ok_resets(tmp_path):
    db = DraftDatabase(str(tmp_path / 't.db'))
    db.record_source_health('rss', 'k', 'A', 'g', 'error', 'x')
    db.record_source_health('rss', 'k', 'A', 'g', 'ok', '')
    row = db.get_source_health('rss', 'k')
    assert int(row['consecutive_errors']) == 0


def test_managed_rss_collection_updates_and_resets_health(tmp_path, monkeypatch):
    db = DraftDatabase(str(tmp_path / "t.db"))
    feed_url = "https://example.com/feed.xml"
    source_id = db.create_managed_source("rss", "Managed", feed_url, "custom")
    db.record_source_health("rss", feed_url, "Managed", "custom", "error", "temporary")

    xml = "<rss><channel><item><title>Useful AI tool update</title><link>https://example.com/post</link></item></channel></rss>"

    class Resp:
        text = xml

        def raise_for_status(self):
            return None

    monkeypatch.setenv("ENABLE_REDDIT_SOURCES", "false")
    monkeypatch.setenv("ENABLE_X_SOURCES", "false")
    monkeypatch.setenv("CUSTOM_TOPIC_FEEDS", "")
    for name in ("OFFICIAL_AI_RSS", "TECH_MEDIA_RSS", "RU_TECH_RSS", "TOOLS_RSS"):
        monkeypatch.setattr(sources, name, [])
    monkeypatch.setattr(sources.requests, "get", lambda *args, **kwargs: Resp())
    monkeypatch.setattr(
        sources,
        "fetch_vc_ru_ai_topics",
        lambda: ([], SourceReport("vc.ru AI", "https://vc.ru/ai", "ru_tech", "empty")),
    )
    monkeypatch.setattr(sources, "_fetch_github_trending_ai", lambda: [])

    items, reports = collect_topics_with_diagnostics(
        settings=SimpleNamespace(enable_telegram_channel_sources=False),
        db=db,
    )

    assert any(item.url == "https://example.com/post" for item in items)
    assert any(report.name == "Managed" and report.status == "ok" for report in reports)
    health = db.get_source_health("rss", feed_url)
    assert health is not None
    assert health["last_status"] == "ok"
    assert int(health["consecutive_errors"]) == 0
    assert db.get_managed_source(source_id)["last_status"] == "ok"


def test_run_async_preserves_runtime_error():
    async def fail():
        raise RuntimeError("original coroutine failure")

    with pytest.raises(RuntimeError, match="original coroutine failure"):
        sources._run_async(fail())


def test_candidate_registry_imports():
    assert any(c.name == 'vc.ru AI' for c in CANDIDATE_SOURCES)


def test_render_source_health_no_secrets(tmp_path):
    db = DraftDatabase(str(tmp_path / 't.db'))
    db.record_source_health('rss', 'k', 'BOT_TOKEN source', 'g', 'error', 'OPENAI_API_KEY')
    text = _render_sources_health(db)
    assert 'OPENAI_API_KEY' not in text


def test_vc_parser_empty_html(monkeypatch):
    class Resp:
        text = '<html><body>none</body></html>'
        def raise_for_status(self):
            return None
    monkeypatch.setattr('bot.sources.requests.get', lambda *a, **k: Resp())
    items, rep = fetch_vc_ru_ai_topics()
    assert items == []
    assert rep.status in {'empty', 'ok'}


def test_vc_parser_extracts_topics_from_fixture(monkeypatch):
    html = Path("tests/fixtures/vc_ru_ai_sample.html").read_text(encoding="utf-8")

    class Resp:
        text = html

        def raise_for_status(self):
            return None

    monkeypatch.setattr("bot.sources.requests.get", lambda *a, **k: Resp())
    items, rep = fetch_vc_ru_ai_topics(max_items=20)
    assert rep.status == "ok"
    assert len(items) >= 2
    urls = {item.url for item in items}
    assert "https://vc.ru/ai/201001" in urls
    assert "https://vc.ru/ai/201002" in urls
    for item in items:
        assert item.title
        assert item.url.startswith("https://vc.ru/")
        assert item.original_description
        assert item.source == "vc.ru AI"
        assert item.source_group == "ru_tech"
        assert item.category
        assert isinstance(item.score, int)


def test_vc_parser_skips_noise_and_deduplicates(monkeypatch):
    html = Path("tests/fixtures/vc_ru_ai_sample.html").read_text(encoding="utf-8")

    class Resp:
        text = html

        def raise_for_status(self):
            return None

    monkeypatch.setattr("bot.sources.requests.get", lambda *a, **k: Resp())
    items, _rep = fetch_vc_ru_ai_topics(max_items=20)
    urls = [item.url for item in items]
    assert urls.count("https://vc.ru/ai/201002") == 1
    assert "https://vc.ru/ai" not in urls
    assert "https://vc.ru/tag/startups" not in urls
    assert "https://vc.ru/tag/ai" not in urls
    assert "https://vc.ru/u/777" not in urls
    assert "https://vc.ru/u/999" not in urls
    assert "https://vc.ru/ai/201001#comments" not in urls
    assert "https://vc.ru/auth/login" not in urls
    assert "https://vc.ru/images/logo.png" not in urls
