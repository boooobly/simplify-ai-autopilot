from __future__ import annotations

import tempfile
from pathlib import Path

from bot.database import DraftDatabase


EXPECTED_INDEXES = {
    "idx_drafts_status_scheduled_at",
    "idx_drafts_source_url",
    "idx_drafts_status_updated_at",
    "idx_topic_candidates_status_score_created_at",
    "idx_topic_candidates_normalized_title",
    "idx_ai_usage_created_at",
}


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


def _assert_database_settings(db: DraftDatabase) -> None:
    with db._connect() as conn:
        indexes = {
            row["name"]
            for table in ("drafts", "topic_candidates", "ai_usage")
            for row in conn.execute(f"PRAGMA index_list({table})").fetchall()
        }
        assert EXPECTED_INDEXES <= indexes
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        topic_columns = {row["name"] for row in conn.execute("PRAGMA table_info(topic_candidates)").fetchall()}
        assert {"title_ru", "reason_ru"} <= topic_columns


def _assert_basic_draft_flow(db: DraftDatabase) -> None:
    draft_id = db.create_draft("test", "https://example.com/post")
    assert db.find_by_source_url("https://example.com/post")["id"] == draft_id

    db.schedule_draft(draft_id, "2000-01-01 00:00:00")
    scheduled = db.get_draft(draft_id)
    assert scheduled is not None
    assert scheduled["status"] == "scheduled"
    assert scheduled["scheduled_at"] == "2000-01-01 00:00:00"

    assert db.mark_draft_publishing(draft_id) is True
    assert db.mark_draft_publishing(draft_id) is False
    publishing_drafts = db.list_publishing_drafts()
    assert [draft["id"] for draft in publishing_drafts] == [draft_id]

    db.mark_draft_failed(draft_id)
    failed = db.get_draft(draft_id)
    assert failed is not None
    assert failed["status"] == "failed"

    assert db.restore_draft(draft_id) is True
    restored = db.get_draft(draft_id)
    assert restored is not None
    assert restored["status"] == "draft"
    assert restored["scheduled_at"] is None
    assert db.restore_draft(draft_id) is False


def _assert_publishing_recovery(db: DraftDatabase) -> None:
    draft_id = db.create_draft("publishing recovery")
    db.schedule_draft(draft_id, "2000-01-01 00:00:00")
    assert db.mark_draft_publishing(draft_id) is True

    recovered_count = db.recover_stuck_publishing_drafts()
    assert recovered_count == 1
    recovered = db.get_draft(draft_id)
    assert recovered is not None
    assert recovered["status"] == "failed"
    assert recovered["scheduled_at"] is None
    assert db.list_publishing_drafts() == []

    assert db.restore_draft(draft_id) is True

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


def _assert_topic_candidates(db: DraftDatabase) -> None:
    r1 = db.upsert_topic_candidate_with_reason(
        "T1", "https://a", "S", None, "news", 50, "r", "same title", "other", "Русский T1", "русская причина"
    )
    assert r1 == "inserted"
    r2 = db.upsert_topic_candidate_with_reason(
        "T2", "https://a", "S", None, "news", 55, "r", "other title", "other"
    )
    assert r2 == "existing_url"
    r3 = db.upsert_topic_candidate_with_reason(
        "T3", "https://b", "S", None, "news", 55, "r", "same title", "other"
    )
    assert r3 == "near_duplicate"

    candidates = db.list_topic_candidates(limit=5)
    assert len(candidates) == 1
    assert candidates[0]["url"] == "https://a"
    assert candidates[0]["title_ru"] == "Русский T1"
    assert candidates[0]["reason_ru"] == "русская причина"
    row_by_url = db.find_topic_candidate_by_url("https://a")
    assert row_by_url is not None
    assert row_by_url["title_ru"] == "Русский T1"
    assert db.update_topic_candidate_display_fields(int(row_by_url["id"]), title_ru="Обновленный T1") is True
    assert db.find_topic_candidate_by_url("https://a")["title_ru"] == "Обновленный T1"
    assert db.create_topic_candidate("English fallback title", "https://fallback", "S", None) is True
    fallback = db.find_topic_candidate_by_url("https://fallback")
    assert fallback is not None
    assert fallback["title_ru"] is None
    assert fallback["title"] == "English fallback title"
    hot_candidates = db.list_topic_candidates_min_score(limit=5, min_score=50)
    assert [candidate["url"] for candidate in hot_candidates] == ["https://a"]


def _assert_ai_usage_summary(db: DraftDatabase) -> None:
    db.record_ai_usage(
        provider="openrouter",
        model="model-a",
        operation="draft",
        prompt_tokens=10,
        completion_tokens=15,
        total_tokens=25,
        estimated_cost_usd=0.01,
    )
    db.record_ai_usage(
        provider="openrouter",
        model="model-b",
        operation="topics",
        prompt_tokens=20,
        completion_tokens=30,
        total_tokens=50,
        estimated_cost_usd=0.02,
    )
    summary = db.get_ai_usage_summary(days=1)
    assert summary["requests"] == 2
    assert summary["prompt_tokens"] == 30
    assert summary["completion_tokens"] == 45
    assert summary["total_tokens"] == 75
    assert abs(summary["estimated_cost_usd"] - 0.03) < 0.000001
    assert [row["model"] for row in summary["by_model"]] == ["model-b", "model-a"]


def run() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.sqlite3"
        db = DraftDatabase(str(db_path))
        _assert_database_settings(db)
        _assert_basic_draft_flow(db)
        _assert_publishing_recovery(db)
        _assert_topic_candidates(db)
        _assert_ai_usage_summary(db)


if __name__ == "__main__":
    run()
