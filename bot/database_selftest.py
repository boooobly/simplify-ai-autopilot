from __future__ import annotations

import tempfile
from pathlib import Path

from bot.database import DraftDatabase


def run() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.sqlite3"
        db = DraftDatabase(str(db_path))

        draft_id = db.create_draft("scheduled")
        db.schedule_draft(draft_id, "2000-01-01 00:00:00")
        assert db.mark_draft_publishing(draft_id) is True
        assert db.mark_draft_publishing(draft_id) is False

        db.mark_draft_published(draft_id)
        draft = db.get_draft(draft_id)
        assert draft is not None
        assert draft["status"] == "published"
        assert draft["scheduled_at"] is None

        failed_id = db.create_draft("will fail")
        db.schedule_draft(failed_id, "2000-01-01 00:00:00")
        assert db.mark_draft_publishing(failed_id) is True
        db.mark_draft_failed(failed_id)
        assert db.get_draft(failed_id)["status"] == "failed"

        assert db.restore_draft(failed_id) is True
        restored = db.get_draft(failed_id)
        assert restored is not None
        assert restored["status"] == "draft"
        assert restored["scheduled_at"] is None
        assert db.restore_draft(failed_id) is False

        stuck_id = db.create_draft("stuck")
        db.update_status(stuck_id, "publishing")
        publishing = db.list_publishing_drafts()
        assert any(int(row["id"]) == stuck_id for row in publishing)
        recovered_ids = db.recover_stuck_publishing_drafts()
        assert stuck_id in recovered_ids
        assert db.get_draft(stuck_id)["status"] == "failed"

        r1 = db.upsert_topic_candidate_with_reason("T1", "https://a", "S", None, "news", 50, "r", "same title", "other")
        assert r1 == "inserted"
        r2 = db.upsert_topic_candidate_with_reason("T2", "https://a", "S", None, "news", 55, "r", "other title", "other")
        assert r2 == "existing_url"
        r3 = db.upsert_topic_candidate_with_reason("T3", "https://b", "S", None, "news", 55, "r", "same title", "other")
        assert r3 == "near_duplicate"


if __name__ == "__main__":
    run()
