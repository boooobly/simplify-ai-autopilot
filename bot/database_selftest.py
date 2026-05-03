from __future__ import annotations

import tempfile
from pathlib import Path

from bot.database import DraftDatabase


def run() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.sqlite3"
        db = DraftDatabase(str(db_path))
        draft_id = db.create_draft("test")
        db.schedule_draft(draft_id, "2000-01-01 00:00:00")
        assert db.mark_draft_publishing(draft_id) is True
        assert db.mark_draft_publishing(draft_id) is False
        db.mark_draft_published(draft_id)
        draft = db.get_draft(draft_id)
        assert draft is not None
        assert draft["status"] == "published"


if __name__ == "__main__":
    run()
