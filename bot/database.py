"""SQLite persistence layer for drafts and moderation status."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


class DraftDatabase:
    """Simple helper class around sqlite3 for draft storage."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    source_url TEXT,
                    media_url TEXT,
                    media_type TEXT,
                    status TEXT NOT NULL DEFAULT 'draft',
                    scheduled_at TIMESTAMP,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self._ensure_column(conn, "drafts", "source_url", "TEXT")
            self._ensure_column(conn, "drafts", "scheduled_at", "TIMESTAMP")
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
            self._ensure_column(conn, "topic_candidates", "reason", "TEXT")
            self._ensure_column(conn, "topic_candidates", "normalized_title", "TEXT")
            self._ensure_column(conn, "topic_candidates", "last_seen_at", "TIMESTAMP")
            self._ensure_column(conn, "topic_candidates", "source_group", "TEXT")
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
            conn.commit()

    def _ensure_column(
        self, conn: sqlite3.Connection, table_name: str, column_name: str, column_sql_type: str
    ) -> None:
        columns = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        existing = {row[1] for row in columns}
        if column_name not in existing:
            conn.execute(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql_type}"
            )

    def create_draft(self, content: str, source_url: str | None = None) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO drafts (content, source_url, status) VALUES (?, ?, 'draft')",
                (content, source_url),
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

    def schedule_draft(self, draft_id: int, scheduled_at_utc: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE drafts
                SET status = 'scheduled', scheduled_at = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (scheduled_at_utc, draft_id),
            )
            conn.commit()

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

    def mark_draft_publishing(self, draft_id: int) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE drafts
                SET status = 'publishing', updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'scheduled'
                """,
                (draft_id,),
            )
            conn.commit()
            return cursor.rowcount == 1

    def mark_draft_published(self, draft_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE drafts
                SET status = 'published', scheduled_at = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (draft_id,),
            )
            conn.commit()

    def mark_draft_failed(self, draft_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE drafts
                SET status = 'failed', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (draft_id,),
            )
            conn.commit()

    def list_publishing_drafts(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM drafts
                WHERE status = 'publishing'
                ORDER BY updated_at ASC, id ASC
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def recover_stuck_publishing_drafts(self) -> list[int]:
        recovered_ids: list[int] = []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id
                FROM drafts
                WHERE status = 'publishing'
                ORDER BY updated_at ASC, id ASC
                """
            ).fetchall()
            if rows:
                recovered_ids = [int(row["id"]) for row in rows]
                conn.execute(
                    """
                    UPDATE drafts
                    SET status = 'failed', updated_at = CURRENT_TIMESTAMP
                    WHERE status = 'publishing'
                    """
                )
            conn.commit()
        return recovered_ids

    def restore_draft(self, draft_id: int) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE drafts
                SET status = 'draft', scheduled_at = NULL, updated_at = CURRENT_TIMESTAMP
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
    ) -> str:
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id, category, score, reason FROM topic_candidates WHERE url = ?",
                (url,),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE topic_candidates
                    SET last_seen_at = CURRENT_TIMESTAMP,
                        category = ?,
                        score = ?,
                        reason = ?,
                        normalized_title = COALESCE(normalized_title, ?),
                        source_group = COALESCE(source_group, ?)
                    WHERE id = ?
                    """,
                    (category, score, reason, normalized_title, source_group, int(existing["id"])),
                )
                conn.commit()
                return "existing_url"
            near_duplicate = conn.execute(
                """
                SELECT id FROM topic_candidates
                WHERE normalized_title = ?
                  AND status != 'rejected'
                LIMIT 1
                """,
                (normalized_title,),
            ).fetchone()
            if near_duplicate:
                return "near_duplicate"
            cursor = conn.execute(
                """
                INSERT INTO topic_candidates (
                    title, url, source, published_at, status, category, score, reason, normalized_title, source_group, created_at, last_seen_at
                )
                VALUES (?, ?, ?, ?, 'new', ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (title, url, source, published_at, category, score, reason, normalized_title, source_group),
            )
            conn.commit()
            return "inserted" if cursor.rowcount > 0 else "existing_url"



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
    ) -> bool:
        return self.upsert_topic_candidate_with_reason(
            title, url, source, published_at, category, score, reason, normalized_title, source_group
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
    def get_topic_candidate(self, topic_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM topic_candidates WHERE id = ?", (topic_id,)).fetchone()
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
