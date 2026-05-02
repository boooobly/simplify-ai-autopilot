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
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()

    def create_draft(self, content: str) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO drafts (content, status) VALUES (?, 'pending')", (content,)
            )
            conn.commit()
            return int(cursor.lastrowid)

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
