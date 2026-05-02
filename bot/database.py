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
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self._ensure_column(conn, "drafts", "source_url", "TEXT")
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
                "INSERT INTO drafts (content, source_url, status) VALUES (?, ?, 'pending')",
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
