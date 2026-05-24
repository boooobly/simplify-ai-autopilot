import asyncio
from types import SimpleNamespace

from bot.database import DraftDatabase
from bot.sources import TopicItem, _with_scoring
import bot.handlers as handlers


def test_collect_uses_managed_sources_path(tmp_path, monkeypatch):
    db = DraftDatabase(str(tmp_path / "db.sqlite"))
    db.create_managed_source("rss", "Managed", "https://example.com/feed.xml", "custom")

    called = {"db": None, "settings": None}

    def fake_collect(settings=None, db=None):
        called["db"] = db
        called["settings"] = settings
        item = _with_scoring(TopicItem(title="Managed topic from rss feed", url="https://example.com/a", source="Managed", source_group="custom"))
        return [item]

    monkeypatch.setattr(handlers, "collect_topics", fake_collect)
    settings = SimpleNamespace(max_topic_age_days=14, has_ai_provider=False)

    stats, items, inserted = asyncio.run(handlers._collect_topics_with_stats(db, settings=settings))

    assert called["db"] is db
    assert called["settings"] is settings
    assert stats.total == 1
    assert len(items) == 1
    assert len(inserted) == 1
