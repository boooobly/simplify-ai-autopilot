from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import bot.publisher as publisher
from bot.database import DraftDatabase
from bot.publisher import PublishResult, run_scheduled_publishing


@pytest.fixture()
def db(tmp_path: Path) -> DraftDatabase:
    return DraftDatabase(str(tmp_path / "drafts.sqlite3"))


def _schedule_due(db: DraftDatabase, content: str = "due draft") -> int:
    draft_id = db.create_draft(content)
    db.schedule_draft(draft_id, "2000-01-01 00:00:00")
    return draft_id


def _context(db: DraftDatabase) -> SimpleNamespace:
    settings = SimpleNamespace(
        channel_id="-100123",
        custom_emoji_map={},
        custom_emoji_aliases={},
    )
    application = SimpleNamespace(bot_data={"settings": settings, "db": db})
    return SimpleNamespace(application=application, bot=object())


def test_only_one_claim_succeeds_for_same_scheduled_draft(db: DraftDatabase) -> None:
    draft_id = _schedule_due(db)

    assert db.mark_draft_publishing(draft_id) is True
    assert db.mark_draft_publishing(draft_id) is False

    draft = db.get_draft(draft_id)
    assert draft is not None
    assert draft["status"] == "publishing"
    assert draft["publishing_started_at"] is not None


def test_publishing_and_published_drafts_are_not_selected_as_due(db: DraftDatabase) -> None:
    publishing_id = _schedule_due(db, "publishing")
    assert db.mark_draft_publishing(publishing_id) is True

    published_id = _schedule_due(db, "published")
    db.mark_draft_published(published_id, channel_id="-100123", message_ids=[501])

    due_id = _schedule_due(db, "still due")

    assert [draft["id"] for draft in db.get_due_scheduled_drafts()] == [due_id]
    published = db.get_draft(published_id)
    assert published is not None
    assert published["published_at"] is not None
    assert published["published_channel_id"] == "-100123"
    assert published["published_message_ids"] == "501"


def test_scheduled_publish_failure_marks_draft_recoverably(
    db: DraftDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft_id = _schedule_due(db)

    async def fail_publish(*args, **kwargs):
        raise RuntimeError("telegram unavailable")

    monkeypatch.setattr(publisher, "publish_to_channel", fail_publish)

    asyncio.run(run_scheduled_publishing(_context(db)))

    draft = db.get_draft(draft_id)
    assert draft is not None
    assert draft["status"] == "failed"
    assert draft["scheduled_at"] is None
    assert draft["publishing_started_at"] is None
    assert draft["publish_error"] == "RuntimeError"
    assert db.restore_draft(draft_id) is True
    assert db.get_draft(draft_id)["status"] == "draft"


def test_scheduler_recovers_stale_publishing_draft_without_resending(
    db: DraftDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft_id = _schedule_due(db)
    assert db.mark_draft_publishing(draft_id) is True
    with db._connect() as conn:
        conn.execute(
            """
            UPDATE drafts
            SET publishing_started_at = datetime('now', '-31 minutes')
            WHERE id = ?
            """,
            (draft_id,),
        )
        conn.commit()

    async def unexpected_publish(*args, **kwargs):
        raise AssertionError("stale publishing draft must not be resent automatically")

    monkeypatch.setattr(publisher, "publish_to_channel", unexpected_publish)

    asyncio.run(run_scheduled_publishing(_context(db)))

    draft = db.get_draft(draft_id)
    assert draft is not None
    assert draft["status"] == "failed"
    assert draft["scheduled_at"] is None
    assert draft["publishing_started_at"] is None
    assert "Recovered from stale publishing state" in draft["publish_error"]


def test_successful_scheduled_publish_records_sent_metadata_and_is_not_reselected(
    db: DraftDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft_id = _schedule_due(db)
    calls: list[str] = []

    async def publish_once(*args, **kwargs):
        calls.append("published")
        return PublishResult(message_ids=[701, 702])

    monkeypatch.setattr(publisher, "publish_to_channel", publish_once)

    asyncio.run(run_scheduled_publishing(_context(db)))
    asyncio.run(run_scheduled_publishing(_context(db)))

    assert calls == ["published"]
    assert db.get_due_scheduled_drafts() == []
    draft = db.get_draft(draft_id)
    assert draft is not None
    assert draft["status"] == "published"
    assert draft["scheduled_at"] is None
    assert draft["published_at"] is not None
    assert draft["published_channel_id"] == "-100123"
    assert draft["published_message_ids"] == "701,702"
