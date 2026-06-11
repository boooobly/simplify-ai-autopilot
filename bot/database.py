"""SQLite persistence layer for drafts and moderation status."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from bot.source_normalization import normalize_source_url, normalize_telegram_channel_input
from bot.topic_scoring import canonical_topic_key, content_format_for_lane, editorial_lane_for_topic, is_similar_topic_key


def _split_related_values(value: str | None) -> list[str]:
    seen: set[str] = set()
    values: list[str] = []
    for raw in (value or "").split("\n"):
        item = raw.strip()
        if item and item not in seen:
            seen.add(item)
            values.append(item)
    return values


def _join_related_values(*values: str | None) -> str:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        for item in _split_related_values(value):
            if item not in seen:
                seen.add(item)
                result.append(item)
    return "\n".join(result)


class DraftDatabase:
    """Simple helper class around sqlite3 for draft storage."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    source_url TEXT,
                    source_image_url TEXT,
                    media_url TEXT,
                    media_type TEXT,
                    status TEXT NOT NULL DEFAULT 'draft',
                    scheduled_at TIMESTAMP,
                    publishing_started_at TIMESTAMP,
                    published_at TIMESTAMP,
                    published_channel_id TEXT,
                    published_message_ids TEXT,
                    publish_error TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self._ensure_column(conn, "drafts", "source_url", "TEXT")
            self._ensure_column(conn, "drafts", "source_image_url", "TEXT")
            self._ensure_column(conn, "drafts", "scheduled_at", "TIMESTAMP")
            self._ensure_column(conn, "drafts", "publishing_started_at", "TIMESTAMP")
            self._ensure_column(conn, "drafts", "published_at", "TIMESTAMP")
            self._ensure_column(conn, "drafts", "published_channel_id", "TEXT")
            self._ensure_column(conn, "drafts", "published_message_ids", "TEXT")
            self._ensure_column(conn, "drafts", "publish_error", "TEXT")
            self._ensure_column(conn, "drafts", "media_url", "TEXT")
            self._ensure_column(conn, "drafts", "media_type", "TEXT")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS topic_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL UNIQUE,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'new',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    published_at TIMESTAMP
                )
                """
            )
            self._ensure_column(conn, "topic_candidates", "category", "TEXT")
            self._ensure_column(conn, "topic_candidates", "score", "INTEGER DEFAULT 0")
            self._ensure_column(conn, "topic_candidates", "deterministic_score", "INTEGER DEFAULT 0")
            self._ensure_column(conn, "topic_candidates", "reason", "TEXT")
            self._ensure_column(conn, "topic_candidates", "title_ru", "TEXT")
            self._ensure_column(conn, "topic_candidates", "summary_ru", "TEXT")
            self._ensure_column(conn, "topic_candidates", "angle_ru", "TEXT")
            self._ensure_column(conn, "topic_candidates", "reason_ru", "TEXT")
            self._ensure_column(conn, "topic_candidates", "original_description", "TEXT")
            self._ensure_column(conn, "topic_candidates", "normalized_title", "TEXT")
            self._ensure_column(conn, "topic_candidates", "last_seen_at", "TIMESTAMP")
            self._ensure_column(conn, "topic_candidates", "source_group", "TEXT")
            self._ensure_column(conn, "topic_candidates", "canonical_key", "TEXT")
            self._ensure_column(conn, "topic_candidates", "related_sources", "TEXT")
            self._ensure_column(conn, "topic_candidates", "related_urls", "TEXT")
            self._ensure_column(conn, "topic_candidates", "related_count", "INTEGER DEFAULT 1")
            self._ensure_column(conn, "topic_candidates", "editorial_lane", "TEXT")
            self._ensure_column(conn, "topic_candidates", "editorial_reason", "TEXT")
            self._ensure_column(conn, "topic_candidates", "content_format", "TEXT")
            self._ensure_column(conn, "topic_candidates", "ai_value_score", "INTEGER")
            self._ensure_column(conn, "topic_candidates", "ai_value_reason_ru", "TEXT")
            self._ensure_column(conn, "topic_candidates", "audience_fit_ru", "TEXT")
            self._ensure_column(conn, "topic_candidates", "metadata_source", "TEXT")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS managed_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    value TEXT NOT NULL,
                    source_group TEXT NOT NULL DEFAULT 'custom',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    last_status TEXT,
                    last_error TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_managed_sources_unique ON managed_sources(source_type, value)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ai_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    prompt_tokens INTEGER DEFAULT 0,
                    completion_tokens INTEGER DEFAULT 0,
                    total_tokens INTEGER DEFAULT 0,
                    estimated_cost_usd REAL DEFAULT 0,
                    source_url TEXT,
                    draft_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS source_health (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_type TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    source_group TEXT NOT NULL DEFAULT 'other',
                    last_status TEXT,
                    last_error TEXT,
                    consecutive_errors INTEGER NOT NULL DEFAULT 0,
                    last_success_at TIMESTAMP,
                    last_checked_at TIMESTAMP,
                    disabled_until TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_source_health_type_key ON source_health(source_type, source_key)")
            self._ensure_indexes(conn)
            conn.commit()

    def _ensure_indexes(self, conn: sqlite3.Connection) -> None:
        index_statements = [
            """
            CREATE INDEX IF NOT EXISTS idx_drafts_status_scheduled_at
            ON drafts (status, scheduled_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_drafts_source_url
            ON drafts (source_url)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_drafts_status_updated_at
            ON drafts (status, updated_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_topic_candidates_status_score_created_at
            ON topic_candidates (status, score, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_topic_candidates_normalized_title
            ON topic_candidates (normalized_title)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_topic_candidates_canonical_key
            ON topic_candidates (canonical_key)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_ai_usage_created_at
            ON ai_usage (created_at)
            """,
        ]
        for statement in index_statements:
            conn.execute(statement)

    def _ensure_column(
        self, conn: sqlite3.Connection, table_name: str, column_name: str, column_sql_type: str
    ) -> None:
        columns = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        existing = {row[1] for row in columns}
        if column_name not in existing:
            conn.execute(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql_type}"
            )


    def _normalize_managed_source_value(self, source_type: str, value: str) -> str:
        source_type = (source_type or "").strip().lower()
        clean = (value or "").strip()
        if source_type == "telegram":
            return normalize_telegram_channel_input(clean).lower()
        if source_type == "rss":
            return normalize_source_url(clean)
        return clean

    def create_managed_source(self, source_type: str, name: str, value: str, source_group: str) -> int:
        source_type = (source_type or "").strip().lower()
        if source_type not in {"rss", "telegram"}:
            raise ValueError("Поддерживаются только rss и telegram источники.")
        normalized_value = self._normalize_managed_source_value(source_type, value)
        if not normalized_value:
            raise ValueError("Пустое значение источника.")
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM managed_sources WHERE source_type = ? AND value = ?",
                (source_type, normalized_value),
            ).fetchone()
            if existing:
                return int(existing["id"])
            cur = conn.execute(
                """
                INSERT INTO managed_sources (source_type, name, value, source_group, enabled)
                VALUES (?, ?, ?, ?, 1)
                """,
                (source_type, name.strip(), normalized_value, source_group.strip() or "custom"),
            )
            conn.commit()
            return int(cur.lastrowid)

    def list_managed_sources(self, include_disabled: bool = True) -> list[sqlite3.Row]:
        with self._connect() as conn:
            if include_disabled:
                return conn.execute("SELECT * FROM managed_sources ORDER BY id DESC").fetchall()
            return conn.execute("SELECT * FROM managed_sources WHERE enabled = 1 ORDER BY id DESC").fetchall()

    def get_managed_source(self, source_id: int) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM managed_sources WHERE id = ?", (source_id,)).fetchone()

    def update_managed_source_enabled(self, source_id: int, enabled: bool) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE managed_sources SET enabled = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (1 if enabled else 0, source_id))
            conn.commit()

    def delete_managed_source(self, source_id: int) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM managed_sources WHERE id = ?", (source_id,))
            conn.commit()
            return cursor.rowcount == 1

    def update_managed_source_status(self, source_id: int, status: str, error: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE managed_sources SET last_status = ?, last_error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status.strip()[:32], (error or "").strip()[:200], source_id),
            )
            conn.commit()

    def find_managed_source(self, source_type: str, value: str) -> sqlite3.Row | None:
        source_type = (source_type or "").strip().lower()
        normalized_value = self._normalize_managed_source_value(source_type, value)
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM managed_sources WHERE source_type = ? AND value = ?",
                (source_type, normalized_value),
            ).fetchone()

    @staticmethod
    def _utc_now_str() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def get_source_health(self, source_type: str, source_key: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM source_health WHERE source_type = ? AND source_key = ?",
                ((source_type or "").strip().lower(), (source_key or "").strip().lower()),
            ).fetchone()

    def should_skip_source(self, source_type: str, source_key: str, now: str | None = None) -> tuple[bool, str]:
        row = self.get_source_health(source_type, source_key)
        if not row or not row["disabled_until"]:
            return False, ""
        now_dt = datetime.strptime(now or self._utc_now_str(), "%Y-%m-%d %H:%M:%S")
        until_dt = datetime.strptime(str(row["disabled_until"]), "%Y-%m-%d %H:%M:%S")
        if until_dt > now_dt:
            return True, f"Источник на паузе до {until_dt.strftime('%H:%M')}"
        return False, ""

    def record_source_health(
        self,
        source_type: str,
        source_key: str,
        source_name: str,
        source_group: str,
        status: str,
        error: str = "",
    ) -> None:
        source_type_n = (source_type or "").strip().lower()
        source_key_n = (source_key or "").strip().lower()
        now = self._utc_now_str()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT consecutive_errors FROM source_health WHERE source_type = ? AND source_key = ?",
                (source_type_n, source_key_n),
            ).fetchone()
            current_errors = int(row["consecutive_errors"]) if row else 0
            consecutive_errors = current_errors
            disabled_until = None
            last_success_at = None
            if status == "ok":
                consecutive_errors = 0
                last_success_at = now
            elif status == "empty":
                consecutive_errors = 0
            elif status == "error":
                consecutive_errors = current_errors + 1
                cool_hours = 24 if consecutive_errors >= 6 else 6 if consecutive_errors >= 3 else 0
                if cool_hours:
                    disabled_until = (datetime.now(timezone.utc) + timedelta(hours=cool_hours)).strftime("%Y-%m-%d %H:%M:%S")
            elif status == "skipped":
                if row:
                    current = conn.execute(
                        "SELECT disabled_until FROM source_health WHERE source_type = ? AND source_key = ?",
                        (source_type_n, source_key_n),
                    ).fetchone()
                    disabled_until = current["disabled_until"] if current else None

            conn.execute(
                """
                INSERT INTO source_health (
                    source_type, source_key, source_name, source_group, last_status, last_error,
                    consecutive_errors, last_success_at, last_checked_at, disabled_until, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_type, source_key) DO UPDATE SET
                    source_name=excluded.source_name,
                    source_group=excluded.source_group,
                    last_status=excluded.last_status,
                    last_error=excluded.last_error,
                    consecutive_errors=excluded.consecutive_errors,
                    last_success_at=COALESCE(excluded.last_success_at, source_health.last_success_at),
                    last_checked_at=excluded.last_checked_at,
                    disabled_until=excluded.disabled_until,
                    updated_at=excluded.updated_at
                """,
                (
                    source_type_n,
                    source_key_n,
                    (source_name or "").strip()[:120],
                    (source_group or "other").strip()[:40],
                    status,
                    (error or "").strip()[:300],
                    consecutive_errors,
                    last_success_at,
                    now,
                    disabled_until,
                    now,
                ),
            )
            conn.commit()

    def list_source_health(self, limit: int = 100) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM source_health ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(500, int(limit))),),
            ).fetchall()


    @staticmethod
    def _cleanup_counts_template() -> dict[str, int]:
        return {
            "old_rejected_topics": 0,
            "old_used_topics": 0,
            "old_new_topics": 0,
            "old_rejected_drafts": 0,
            "old_failed_drafts": 0,
            "stale_draft_drafts": 0,
            "total": 0,
        }

    @staticmethod
    def _topic_cleanup_where(status: str, age_days: int) -> tuple[str, tuple[Any, ...]]:
        age_days = max(1, int(age_days))
        # last_seen_at is the safest activity timestamp for merged/re-seen topics.
        # Fall back to created_at only when last_seen_at is empty. datetime(...)
        # returns NULL for missing/unparsable values, and those rows are excluded.
        where_sql = """
            status = ?
            AND datetime(COALESCE(NULLIF(last_seen_at, ''), NULLIF(created_at, ''))) IS NOT NULL
            AND datetime(COALESCE(NULLIF(last_seen_at, ''), NULLIF(created_at, ''))) < datetime('now', ?)
        """
        return where_sql, (status, f"-{age_days} days")

    @staticmethod
    def _draft_cleanup_where(status: str, age_days: int, *, stale_draft: bool = False) -> tuple[str, tuple[Any, ...]]:
        age_days = max(1, int(age_days))
        clauses = [
            "status = ?",
            "datetime(NULLIF(updated_at, '')) IS NOT NULL",
            "datetime(NULLIF(updated_at, '')) < datetime('now', ?)",
            "COALESCE(NULLIF(media_url, ''), NULLIF(media_type, '')) IS NULL",
        ]
        if stale_draft:
            clauses.append("scheduled_at IS NULL")
        return " AND ".join(clauses), (status, f"-{age_days} days")

    def cleanup_preview(
        self,
        *,
        topic_age_days: int = 30,
        rejected_draft_age_days: int = 30,
        failed_draft_age_days: int = 30,
        stale_draft_age_days: int = 45,
    ) -> dict[str, int]:
        """Return conservative cleanup counts without deleting any rows."""

        counts = self._cleanup_counts_template()
        queries = [
            ("old_rejected_topics", "topic_candidates", self._topic_cleanup_where("rejected", topic_age_days)),
            ("old_used_topics", "topic_candidates", self._topic_cleanup_where("used", topic_age_days)),
            ("old_new_topics", "topic_candidates", self._topic_cleanup_where("new", topic_age_days)),
            ("old_rejected_drafts", "drafts", self._draft_cleanup_where("rejected", rejected_draft_age_days)),
            ("old_failed_drafts", "drafts", self._draft_cleanup_where("failed", failed_draft_age_days)),
            ("stale_draft_drafts", "drafts", self._draft_cleanup_where("draft", stale_draft_age_days, stale_draft=True)),
        ]
        with self._connect() as conn:
            for key, table, (where_sql, params) in queries:
                row = conn.execute(f"SELECT COUNT(*) AS count FROM {table} WHERE {where_sql}", params).fetchone()
                counts[key] = int(row["count"] if row else 0)
        counts["total"] = sum(value for key, value in counts.items() if key != "total")
        return counts

    def cleanup_apply(
        self,
        *,
        topic_age_days: int = 30,
        rejected_draft_age_days: int = 30,
        failed_draft_age_days: int = 30,
        stale_draft_age_days: int = 45,
    ) -> dict[str, int]:
        """Delete only rows eligible under the conservative cleanup rules."""

        counts = self._cleanup_counts_template()
        deletions = [
            ("old_rejected_topics", "topic_candidates", self._topic_cleanup_where("rejected", topic_age_days)),
            ("old_used_topics", "topic_candidates", self._topic_cleanup_where("used", topic_age_days)),
            ("old_new_topics", "topic_candidates", self._topic_cleanup_where("new", topic_age_days)),
            ("old_rejected_drafts", "drafts", self._draft_cleanup_where("rejected", rejected_draft_age_days)),
            ("old_failed_drafts", "drafts", self._draft_cleanup_where("failed", failed_draft_age_days)),
            ("stale_draft_drafts", "drafts", self._draft_cleanup_where("draft", stale_draft_age_days, stale_draft=True)),
        ]
        with self._connect() as conn:
            for key, table, (where_sql, params) in deletions:
                cursor = conn.execute(f"DELETE FROM {table} WHERE {where_sql}", params)
                counts[key] = int(cursor.rowcount if cursor.rowcount is not None else 0)
            conn.commit()
        counts["total"] = sum(value for key, value in counts.items() if key != "total")
        return counts

    def create_draft(
        self,
        content: str,
        source_url: str | None = None,
        source_image_url: str | None = None,
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO drafts (content, source_url, source_image_url, status)
                VALUES (?, ?, ?, 'draft')
                """,
                (content, source_url, source_image_url),
            )
            conn.commit()
            return int(cursor.lastrowid)

    def find_by_source_url(self, source_url: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM drafts
                WHERE source_url = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (source_url,),
            ).fetchone()
            return dict(row) if row else None

    def get_draft(self, draft_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
            return dict(row) if row else None

    def update_draft_content(self, draft_id: int, content: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE drafts
                SET content = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (content, draft_id),
            )
            conn.commit()

    def update_draft_source_image_url(self, draft_id: int, source_image_url: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE drafts
                SET source_image_url = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (source_image_url, draft_id),
            )
            conn.commit()

    def attach_media(self, draft_id: int, media_url: str, media_type: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE drafts
                SET media_url = ?, media_type = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (media_url, media_type, draft_id),
            )
            conn.commit()

    def clear_media(self, draft_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE drafts
                SET media_url = NULL, media_type = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (draft_id,),
            )
            conn.commit()

    def update_status(self, draft_id: int, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE drafts
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, draft_id),
            )
            conn.commit()

    def schedule_draft(self, draft_id: int, scheduled_at_utc: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE drafts
                SET status = 'scheduled',
                    scheduled_at = ?,
                    publishing_started_at = NULL,
                    publish_error = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                  AND status IN ('draft', 'approved', 'scheduled')
                  AND NOT EXISTS (
                      SELECT 1
                      FROM drafts AS occupied
                      WHERE occupied.id != ?
                        AND occupied.status = 'scheduled'
                        AND occupied.scheduled_at = ?
                  )
                """,
                (scheduled_at_utc, draft_id, draft_id, scheduled_at_utc),
            )
            conn.commit()
            return cursor.rowcount == 1

    def get_due_scheduled_drafts(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM drafts
                WHERE status = 'scheduled'
                  AND scheduled_at IS NOT NULL
                  AND scheduled_at <= CURRENT_TIMESTAMP
                ORDER BY scheduled_at ASC
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def list_scheduled_drafts_between(self, start_iso: str, end_iso: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM drafts
                WHERE status = 'scheduled'
                  AND scheduled_at IS NOT NULL
                  AND scheduled_at >= ?
                  AND scheduled_at < ?
                ORDER BY scheduled_at ASC
                """,
                (start_iso, end_iso),
            ).fetchall()
            return [dict(row) for row in rows]

    def unschedule_draft(self, draft_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE drafts
                SET status = 'draft', scheduled_at = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (draft_id,),
            )
            conn.commit()

    def mark_draft_publishing(
        self,
        draft_id: int,
        allowed_statuses: tuple[str, ...] = ("scheduled",),
    ) -> bool:
        statuses = tuple(dict.fromkeys(status.strip() for status in allowed_statuses if status.strip()))
        if not statuses:
            return False
        placeholders = ", ".join("?" for _ in statuses)
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE drafts
                SET status = 'publishing',
                    publishing_started_at = CURRENT_TIMESTAMP,
                    publish_error = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status IN ({placeholders})
                """,
                (draft_id, *statuses),
            )
            conn.commit()
            return cursor.rowcount == 1

    def mark_draft_published(
        self,
        draft_id: int,
        *,
        channel_id: str | None = None,
        message_ids: list[int] | None = None,
    ) -> None:
        serialized_message_ids = ",".join(str(message_id) for message_id in (message_ids or [])) or None
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE drafts
                SET status = 'published',
                    scheduled_at = NULL,
                    publishing_started_at = NULL,
                    published_at = COALESCE(published_at, CURRENT_TIMESTAMP),
                    published_channel_id = COALESCE(?, published_channel_id),
                    published_message_ids = COALESCE(?, published_message_ids),
                    publish_error = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (channel_id, serialized_message_ids, draft_id),
            )
            conn.commit()

    def mark_draft_failed(self, draft_id: int, error: str | None = None) -> None:
        stored_error = (error or "").strip()[:1000] or None
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE drafts
                SET status = 'failed',
                    scheduled_at = NULL,
                    publishing_started_at = NULL,
                    publish_error = COALESCE(?, publish_error),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (stored_error, draft_id),
            )
            conn.commit()

    def list_publishing_drafts(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM drafts
                WHERE status = 'publishing'
                ORDER BY updated_at ASC, id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def recover_stuck_publishing_drafts(self, stale_after_minutes: int = 30) -> int:
        cutoff = f"-{max(0, int(stale_after_minutes))} minutes"
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE drafts
                SET status = 'failed',
                    scheduled_at = NULL,
                    publishing_started_at = NULL,
                    publish_error = 'Recovered from stale publishing state; verify Telegram channel before retrying.',
                    updated_at = CURRENT_TIMESTAMP
                WHERE status = 'publishing'
                  AND (publishing_started_at IS NULL OR publishing_started_at <= datetime('now', ?))
                """,
                (cutoff,),
            )
            conn.commit()
            return cursor.rowcount

    def restore_draft(self, draft_id: int) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE drafts
                SET status = 'draft',
                    scheduled_at = NULL,
                    publishing_started_at = NULL,
                    publish_error = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'failed'
                """,
                (draft_id,),
            )
            conn.commit()
            return cursor.rowcount == 1

    def list_drafts(self, limit: int = 10, status: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if status is None:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM drafts
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM drafts
                    WHERE status = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (status, limit),
                ).fetchall()
            return [dict(row) for row in rows]

    def delete_draft(self, draft_id: int) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM drafts WHERE id = ?", (draft_id,))
            conn.commit()
            return cursor.rowcount > 0

    def _merge_topic_candidate_row(
        self,
        conn: sqlite3.Connection,
        existing: sqlite3.Row,
        *,
        title: str,
        url: str,
        source: str,
        category: str,
        score: int,
        reason: str,
        normalized_title: str,
        canonical_key: str,
        source_group: str,
        title_ru: str | None,
        summary_ru: str | None,
        angle_ru: str | None,
        reason_ru: str | None,
        original_description: str | None,
        lane: str,
        lane_reason: str,
        content_format: str,
    ) -> None:
        existing_sources = _join_related_values(existing["related_sources"], existing["source"], source)
        existing_urls = _join_related_values(existing["related_urls"], existing["url"], url)
        related_count = max(1, len(_split_related_values(existing_urls)))
        existing_score = int(existing["score"] or 0)
        if score > existing_score:
            category_to_store = category
            score_to_store = score
            reason_to_store = reason
            title_ru_to_store = title_ru
            summary_ru_to_store = summary_ru
            angle_ru_to_store = angle_ru
            reason_ru_to_store = reason_ru
            original_description_to_store = original_description
        else:
            category_to_store = existing["category"]
            score_to_store = existing_score
            reason_to_store = existing["reason"]
            title_ru_to_store = None
            summary_ru_to_store = None
            angle_ru_to_store = None
            reason_ru_to_store = None
            original_description_to_store = None
        conn.execute(
            """
            UPDATE topic_candidates
            SET last_seen_at = CURRENT_TIMESTAMP,
                category = ?,
                score = ?,
                reason = ?,
                deterministic_score = ?,
                title_ru = COALESCE(NULLIF(?, ''), title_ru),
                summary_ru = COALESCE(NULLIF(?, ''), summary_ru),
                angle_ru = COALESCE(NULLIF(?, ''), angle_ru),
                reason_ru = COALESCE(NULLIF(?, ''), reason_ru),
                original_description = COALESCE(NULLIF(?, ''), original_description),
                normalized_title = COALESCE(NULLIF(normalized_title, ''), ?),
                canonical_key = COALESCE(NULLIF(canonical_key, ''), ?),
                source_group = COALESCE(source_group, ?),
                related_sources = ?,
                related_urls = ?,
                related_count = ?,
                editorial_lane = COALESCE(NULLIF(editorial_lane, ''), ?),
                editorial_reason = COALESCE(NULLIF(editorial_reason, ''), ?),
                content_format = COALESCE(NULLIF(content_format, ''), ?)
            WHERE id = ?
            """,
            (
                category_to_store,
                score_to_store,
                reason_to_store,
                int(score_to_store or 0),
                title_ru_to_store,
                summary_ru_to_store,
                angle_ru_to_store,
                reason_ru_to_store,
                original_description_to_store,
                normalized_title,
                canonical_key,
                source_group,
                existing_sources,
                existing_urls,
                related_count,
                lane,
                lane_reason,
                content_format,
                int(existing["id"]),
            ),
        )

    def _find_similar_topic_candidate(self, conn: sqlite3.Connection, canonical_key: str) -> sqlite3.Row | None:
        if not canonical_key:
            return None
        exact = conn.execute(
            """
            SELECT * FROM topic_candidates
            WHERE canonical_key = ?
              AND status != 'rejected'
            ORDER BY score DESC, created_at DESC
            LIMIT 1
            """,
            (canonical_key,),
        ).fetchone()
        if exact:
            return exact
        rows = conn.execute(
            """
            SELECT * FROM topic_candidates
            WHERE canonical_key IS NOT NULL
              AND canonical_key != ''
              AND status != 'rejected'
              AND created_at >= datetime('now', '-21 days')
            ORDER BY created_at DESC
            LIMIT 200
            """
        ).fetchall()
        for row in rows:
            if is_similar_topic_key(canonical_key, str(row["canonical_key"] or "")):
                return row
        return None

    def upsert_topic_candidate_with_reason(
        self,
        title: str,
        url: str,
        source: str,
        published_at: str | None,
        category: str,
        score: int,
        reason: str,
        normalized_title: str,
        source_group: str = "other",
        title_ru: str | None = None,
        summary_ru: str | None = None,
        angle_ru: str | None = None,
        reason_ru: str | None = None,
        original_description: str | None = None,
        canonical_key: str | None = None,
    ) -> str:
        canonical_key = (canonical_key or canonical_topic_key(title, source_group)).strip()
        lane, lane_reason = editorial_lane_for_topic(title, source, url, source_group, original_description, category, score)
        content_format = content_format_for_lane(lane, score)
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM topic_candidates WHERE url = ?",
                (url,),
            ).fetchone()
            if existing:
                self._merge_topic_candidate_row(
                    conn,
                    existing,
                    title=title,
                    url=url,
                    source=source,
                    category=category,
                    score=score,
                    reason=reason,
                    normalized_title=normalized_title,
                    canonical_key=canonical_key,
                    source_group=source_group,
                    title_ru=title_ru,
                    summary_ru=summary_ru,
                    angle_ru=angle_ru,
                    reason_ru=reason_ru,
                    original_description=original_description,
                    lane=lane,
                    lane_reason=lane_reason,
                    content_format=content_format,
                )
                conn.commit()
                return "existing_url"

            similar = self._find_similar_topic_candidate(conn, canonical_key)
            if not similar and normalized_title:
                similar = conn.execute(
                    """
                    SELECT * FROM topic_candidates
                    WHERE normalized_title = ?
                      AND status != 'rejected'
                    ORDER BY score DESC, created_at DESC
                    LIMIT 1
                    """,
                    (normalized_title,),
                ).fetchone()
            if similar:
                self._merge_topic_candidate_row(
                    conn,
                    similar,
                    title=title,
                    url=url,
                    source=source,
                    category=category,
                    score=score,
                    reason=reason,
                    normalized_title=normalized_title,
                    canonical_key=canonical_key,
                    source_group=source_group,
                    title_ru=title_ru,
                    summary_ru=summary_ru,
                    angle_ru=angle_ru,
                    reason_ru=reason_ru,
                    original_description=original_description,
                    lane=lane,
                    lane_reason=lane_reason,
                    content_format=content_format,
                )
                conn.commit()
                return "merged_story"

            cursor = conn.execute(
                """
                INSERT INTO topic_candidates (
                    title, url, source, published_at, status, category, score, deterministic_score, reason,
                    title_ru, summary_ru, angle_ru, reason_ru, original_description,
                    normalized_title, source_group, canonical_key, related_sources,
                    related_urls, related_count, editorial_lane, editorial_reason, content_format, created_at, last_seen_at
                )
                VALUES (?, ?, ?, ?, 'new', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (
                    title,
                    url,
                    source,
                    published_at,
                    category,
                    score,
                    score,
                    reason,
                    title_ru,
                    summary_ru,
                    angle_ru,
                    reason_ru,
                    original_description,
                    normalized_title,
                    source_group,
                    canonical_key,
                    source,
                    url,
                    lane,
                    lane_reason,
                    content_format,
                ),
            )
            conn.commit()
            return "inserted" if cursor.rowcount > 0 else "existing_url"

    def update_topic_candidate_display_fields(
        self,
        topic_id: int,
        title_ru: str | None = None,
        summary_ru: str | None = None,
        angle_ru: str | None = None,
        reason_ru: str | None = None,
        score: int | None = None,
        content_format: str | None = None,
        ai_value_score: int | None = None,
        ai_value_reason_ru: str | None = None,
        audience_fit_ru: str | None = None,
        clear_ai_value: bool = False,
        metadata_source: str | None = None,
    ) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE topic_candidates
                SET title_ru = COALESCE(NULLIF(?, ''), title_ru),
                    summary_ru = COALESCE(NULLIF(?, ''), summary_ru),
                    angle_ru = COALESCE(NULLIF(?, ''), angle_ru),
                    reason_ru = COALESCE(NULLIF(?, ''), reason_ru),
                    score = COALESCE(?, score),
                    content_format = COALESCE(NULLIF(?, ''), content_format),
                    ai_value_score = CASE WHEN ? THEN NULL ELSE COALESCE(?, ai_value_score) END,
                    ai_value_reason_ru = CASE WHEN ? THEN NULL ELSE COALESCE(NULLIF(?, ''), ai_value_reason_ru) END,
                    audience_fit_ru = CASE WHEN ? THEN NULL ELSE COALESCE(NULLIF(?, ''), audience_fit_ru) END,
                    metadata_source = COALESCE(NULLIF(?, ''), metadata_source)
                WHERE id = ?
                """,
                (
                    title_ru,
                    summary_ru,
                    angle_ru,
                    reason_ru,
                    score,
                    content_format,
                    clear_ai_value,
                    ai_value_score,
                    clear_ai_value,
                    ai_value_reason_ru,
                    clear_ai_value,
                    audience_fit_ru,
                    metadata_source,
                    topic_id,
                ),
            )
            conn.commit()
            return cursor.rowcount > 0

    def force_update_topic_candidate_display_fields(
        self,
        topic_id: int,
        title_ru: str,
        summary_ru: str,
        angle_ru: str,
        reason_ru: str,
        score: int | None = None,
        content_format: str | None = None,
        ai_value_score: int | None = None,
        ai_value_reason_ru: str | None = None,
        audience_fit_ru: str | None = None,
        clear_ai_value: bool = False,
        metadata_source: str | None = None,
    ) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE topic_candidates
                SET title_ru = ?,
                    summary_ru = ?,
                    angle_ru = ?,
                    reason_ru = ?,
                    score = COALESCE(?, score),
                    content_format = COALESCE(NULLIF(?, ''), content_format),
                    ai_value_score = CASE WHEN ? THEN NULL ELSE COALESCE(?, ai_value_score) END,
                    ai_value_reason_ru = CASE WHEN ? THEN NULL ELSE COALESCE(NULLIF(?, ''), ai_value_reason_ru) END,
                    audience_fit_ru = CASE WHEN ? THEN NULL ELSE COALESCE(NULLIF(?, ''), audience_fit_ru) END,
                    metadata_source = COALESCE(NULLIF(?, ''), metadata_source)
                WHERE id = ?
                """,
                (
                    title_ru,
                    summary_ru,
                    angle_ru,
                    reason_ru,
                    score,
                    content_format,
                    clear_ai_value,
                    ai_value_score,
                    clear_ai_value,
                    ai_value_reason_ru,
                    clear_ai_value,
                    audience_fit_ru,
                    metadata_source,
                    topic_id,
                ),
            )
            conn.commit()
            return cursor.rowcount > 0



    def upsert_topic_candidate(
        self,
        title: str,
        url: str,
        source: str,
        published_at: str | None,
        category: str,
        score: int,
        reason: str,
        normalized_title: str,
        source_group: str = "other",
        title_ru: str | None = None,
        summary_ru: str | None = None,
        angle_ru: str | None = None,
        reason_ru: str | None = None,
        original_description: str | None = None,
    ) -> bool:
        return self.upsert_topic_candidate_with_reason(
            title, url, source, published_at, category, score, reason, normalized_title, source_group, title_ru, summary_ru, angle_ru, reason_ru, original_description
        ) == "inserted"
    def create_topic_candidate(self, title: str, url: str, source: str, published_at: str | None, source_group: str = "other") -> bool:
        return self.upsert_topic_candidate(
            title=title,
            url=url,
            source=source,
            published_at=published_at,
            category="other",
            score=0,
            reason="Без оценки",
            normalized_title=title.strip().lower(),
            source_group=source_group,
        )

    def list_topic_candidates(
        self, limit: int = 10, status: str | None = "new", order_by_score: bool = True
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            where_clause = "WHERE status = ?" if status is not None else ""
            params: tuple[Any, ...] = (status, limit) if status is not None else (limit,)
            order_by = "ORDER BY score DESC, created_at DESC" if order_by_score else "ORDER BY created_at DESC"
            rows = conn.execute(
                """
                SELECT *
                FROM topic_candidates
                """
                + where_clause
                + """
                """
                + order_by
                + """
                LIMIT ?
                """,
                params,
            ).fetchall()
            return [dict(row) for row in rows]



    def list_topic_candidates_filtered(
        self,
        limit: int = 10,
        status: str | None = "new",
        categories: list[str] | None = None,
        source_groups: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            clauses = []
            params: list[Any] = []
            if status is not None:
                clauses.append("status = ?")
                params.append(status)
            if categories:
                placeholders = ",".join(["?"] * len(categories))
                clauses.append(f"category IN ({placeholders})")
                params.extend(categories)
            if source_groups:
                placeholders = ",".join(["?"] * len(source_groups))
                clauses.append(f"source_group IN ({placeholders})")
                params.extend(source_groups)
            where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            params.append(limit)
            rows = conn.execute(
                "SELECT * FROM topic_candidates " + where_clause + " ORDER BY score DESC, created_at DESC LIMIT ?",
                tuple(params),
            ).fetchall()
            return [dict(row) for row in rows]


    def list_topic_candidates_by_editorial(
        self,
        limit: int = 10,
        status: str | None = "new",
        lanes: list[str] | None = None,
        formats: list[str] | None = None,
        categories: list[str] | None = None,
        min_score: int = 0,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            clauses = []
            params: list[Any] = []
            if status is not None:
                clauses.append("status = ?")
                params.append(status)
            if lanes:
                clauses.append(f"editorial_lane IN ({','.join(['?'] * len(lanes))})")
                params.extend(lanes)
            if formats:
                clauses.append(f"content_format IN ({','.join(['?'] * len(formats))})")
                params.extend(formats)
            if categories:
                clauses.append(f"category IN ({','.join(['?'] * len(categories))})")
                params.extend(categories)
            if min_score > 0:
                clauses.append("score >= ?")
                params.append(min_score)
            where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            params.append(limit)
            rows = conn.execute("SELECT * FROM topic_candidates " + where_clause + " ORDER BY score DESC, created_at DESC LIMIT ?", tuple(params)).fetchall()
            return [dict(r) for r in rows]

    def list_topic_candidates_min_score(self, limit: int = 15, status: str = "new", min_score: int = 75) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM topic_candidates
                WHERE status = ? AND score >= ?
                ORDER BY score DESC, created_at DESC
                LIMIT ?
                """,
                (status, min_score, limit),
            ).fetchall()
            return [dict(row) for row in rows]
    def get_balanced_topic_shortlist(self, limit: int = 12, hours: int = 48, min_score: int = 60) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM topic_candidates
                WHERE status = 'new'
                  AND score >= ?
                  AND created_at >= datetime('now', ?)
                ORDER BY score DESC, related_count DESC, created_at DESC
                LIMIT 300
                """,
                (min_score, f'-{max(1, int(hours))} hours'),
            ).fetchall()
        topics = [dict(r) for r in rows]
        lane_limits = {"tool": 3, "creator": 3, "breaking_news": 2, "short_video": 2, "meme": 1, "guide": 1}
        source_cap = 3
        lane_counts: dict[str, int] = {}
        source_counts: dict[str, int] = {}
        selected: list[dict[str, Any]] = []
        for topic in topics:
            if len(selected) >= limit:
                break
            lane = str(topic.get("editorial_lane") or "")
            source = str(topic.get("source") or "")
            if source_counts.get(source, 0) >= source_cap:
                continue
            if lane in lane_limits and lane_counts.get(lane, 0) >= lane_limits[lane]:
                continue
            selected.append(topic)
            source_counts[source] = source_counts.get(source, 0) + 1
            lane_counts[lane] = lane_counts.get(lane, 0) + 1
        return selected

    def get_topic_candidate(self, topic_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM topic_candidates WHERE id = ?", (topic_id,)).fetchone()
            return dict(row) if row else None

    def find_topic_candidate_by_url(self, url: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM topic_candidates WHERE url = ?", (url,)).fetchone()
            return dict(row) if row else None

    def update_topic_status(self, topic_id: int, status: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE topic_candidates SET status = ? WHERE id = ?", (status, topic_id))
            conn.commit()

    def record_ai_usage(
        self,
        *,
        provider: str,
        model: str,
        operation: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        estimated_cost_usd: float = 0.0,
        source_url: str | None = None,
        draft_id: int | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO ai_usage (
                    provider, model, operation, prompt_tokens, completion_tokens, total_tokens,
                    estimated_cost_usd, source_url, draft_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    provider,
                    model,
                    operation,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    estimated_cost_usd,
                    source_url,
                    draft_id,
                ),
            )
            conn.commit()

    def get_ai_usage_summary(self, days: int = 1) -> dict[str, Any]:
        with self._connect() as conn:
            total_row = conn.execute(
                """
                SELECT
                    COUNT(*) AS requests,
                    COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                    COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd
                FROM ai_usage
                WHERE created_at >= datetime('now', ?)
                """,
                (f"-{max(1, int(days))} days",),
            ).fetchone()
            model_rows = conn.execute(
                """
                SELECT
                    model,
                    COUNT(*) AS requests,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd
                FROM ai_usage
                WHERE created_at >= datetime('now', ?)
                GROUP BY model
                ORDER BY total_tokens DESC
                """,
                (f"-{max(1, int(days))} days",),
            ).fetchall()
            return {
                "requests": int(total_row["requests"] if total_row else 0),
                "prompt_tokens": int(total_row["prompt_tokens"] if total_row else 0),
                "completion_tokens": int(total_row["completion_tokens"] if total_row else 0),
                "total_tokens": int(total_row["total_tokens"] if total_row else 0),
                "estimated_cost_usd": float(total_row["estimated_cost_usd"] if total_row else 0.0),
                "by_model": [dict(row) for row in model_rows],
            }
