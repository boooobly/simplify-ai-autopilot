from pathlib import Path

from bot.database import DraftDatabase
from bot.sources import fetch_vc_ru_ai_topics
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
