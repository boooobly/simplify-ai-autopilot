from bot.database import DraftDatabase
from bot.sources import collect_topics_with_diagnostics, fetch_vc_ru_ai_topics
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
