from __future__ import annotations

import tempfile
from pathlib import Path

from bot.database import DraftDatabase


def _create_with_status(db: DraftDatabase, status: str) -> int:
    draft_id = db.create_draft(f"test {status}")
    if status == "draft":
        return draft_id
    if status == "scheduled":
        db.schedule_draft(draft_id, "2030-01-01 00:00:00")
        return draft_id
    if status == "publishing":
        db.schedule_draft(draft_id, "2030-01-01 00:00:00")
        assert db.mark_draft_publishing(draft_id) is True
        return draft_id
    if status == "published":
        db.mark_draft_published(draft_id)
        return draft_id
    db.update_status(draft_id, status)
    return draft_id


def run() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.sqlite3"
        db = DraftDatabase(str(db_path))
        draft_id = db.create_draft("test")
        db.schedule_draft(draft_id, "2000-01-01 00:00:00")
        assert db.mark_draft_publishing(draft_id) is True
        assert db.mark_draft_publishing(draft_id) is False
        publishing_drafts = db.list_publishing_drafts()
        assert [draft["id"] for draft in publishing_drafts] == [draft_id]

        recovered_count = db.recover_stuck_publishing_drafts()
        assert recovered_count == 1
        recovered = db.get_draft(draft_id)
        assert recovered is not None
        assert recovered["status"] == "failed"
        assert recovered["scheduled_at"] is None
        assert db.list_publishing_drafts() == []

        assert db.restore_draft(draft_id) is True
        restored = db.get_draft(draft_id)
        assert restored is not None
        assert restored["status"] == "draft"
        assert restored["scheduled_at"] is None
        assert db.restore_draft(draft_id) is False

        failed_with_schedule_id = db.create_draft("failed with stale schedule")
        db.schedule_draft(failed_with_schedule_id, "2030-01-01 00:00:00")
        db.update_status(failed_with_schedule_id, "failed")
        assert db.restore_draft(failed_with_schedule_id) is True
        failed_restored = db.get_draft(failed_with_schedule_id)
        assert failed_restored is not None
        assert failed_restored["status"] == "draft"
        assert failed_restored["scheduled_at"] is None

        for status in ["scheduled", "published", "rejected", "draft", "publishing"]:
            status_id = _create_with_status(db, status)
            before = db.get_draft(status_id)
            assert before is not None
            assert db.restore_draft(status_id) is False
            after = db.get_draft(status_id)
            assert after is not None
            assert after["status"] == before["status"]
            assert after["scheduled_at"] == before["scheduled_at"]

        r1 = db.upsert_topic_candidate_with_reason("T1", "https://a", "S", None, "news", 50, "r", "same title", "other")
        assert r1 == "inserted"
        r2 = db.upsert_topic_candidate_with_reason("T2", "https://a", "S", None, "news", 55, "r", "other title", "other")
        assert r2 == "existing_url"
        r3 = db.upsert_topic_candidate_with_reason("T3", "https://b", "S", None, "news", 55, "r", "same title", "other")
        assert r3 == "near_duplicate"


if __name__ == "__main__":
    run()
