"""Durable, local conversation history for the voice UI."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_REHYDRATE_MESSAGES = 40
DEFAULT_REHYDRATE_MAX_CHARS = 6000


@dataclass(frozen=True)
class ContextWindow:
    """The bounded transcript replayed to the model on reconnect."""

    messages: list[dict[str, str]]
    truncated: bool
    oldest_rowid: int | None


def _default_db_path() -> Path:
    configured = os.getenv("CAAL_CONVERSATIONS_DB")
    if configured:
        return Path(configured)
    data_dir = Path(os.getenv("CAAL_MEMORY_DIR", "data"))
    return data_dir / "conversations.sqlite3"


class ConversationStore:
    """SQLite-backed conversations shared by the webhook and agent processes."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else _default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    summary TEXT,
                    summary_through_rowid INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'tool')),
                    content TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS messages_conversation_created
                    ON messages(conversation_id, created_at);

                CREATE TABLE IF NOT EXISTS conversation_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(conversations)")
            }
            if "summary" not in columns:
                connection.execute("ALTER TABLE conversations ADD COLUMN summary TEXT")
            if "summary_through_rowid" not in columns:
                connection.execute(
                    "ALTER TABLE conversations "
                    "ADD COLUMN summary_through_rowid INTEGER NOT NULL DEFAULT 0"
                )

    @staticmethod
    def _new_id() -> str:
        return uuid.uuid4().hex

    @staticmethod
    def _title_from_message(content: str) -> str:
        words = content.strip().split()
        title = " ".join(words[:8])
        if len(words) > 8:
            title += "…"
        return title[:64] or "New conversation"

    def _create_locked(self, connection: sqlite3.Connection, title: str) -> str:
        conversation_id = self._new_id()
        now = time.time()
        connection.execute(
            "INSERT INTO conversations(id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (conversation_id, title.strip() or "New conversation", now, now),
        )
        connection.execute(
            "INSERT INTO conversation_state(key, value) VALUES ('active', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (conversation_id,),
        )
        return conversation_id

    def ensure_active(self) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM conversation_state WHERE key = 'active'"
            ).fetchone()
            if row and connection.execute(
                "SELECT 1 FROM conversations WHERE id = ?", (row["value"],)
            ).fetchone():
                return str(row["value"])
            return self._create_locked(connection, "New conversation")

    def create(self, title: str = "New conversation") -> str:
        with self._connect() as connection:
            return self._create_locked(connection, title)

    def activate(self, conversation_id: str) -> None:
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if not exists:
                raise KeyError(conversation_id)
            connection.execute(
                "INSERT INTO conversation_state(key, value) VALUES ('active', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (conversation_id,),
            )

    def list(self) -> dict[str, Any]:
        active_id = self.ensure_active()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT c.id, c.title, c.created_at, c.updated_at,
                       COUNT(m.id) AS message_count
                FROM conversations c
                LEFT JOIN messages m ON m.conversation_id = c.id
                GROUP BY c.id
                ORDER BY c.updated_at DESC
                """
            ).fetchall()
        return {
            "active_id": active_id,
            "conversations": [dict(row) for row in rows],
        }

    def detail(self, conversation_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            conversation = connection.execute(
                "SELECT id, title, created_at, updated_at FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if not conversation:
                raise KeyError(conversation_id)
            messages = connection.execute(
                """
                SELECT id, role, content, metadata, created_at
                FROM messages
                WHERE conversation_id = ?
                ORDER BY created_at, rowid
                """,
                (conversation_id,),
            ).fetchall()
        parsed_messages = []
        for row in messages:
            item = dict(row)
            item["metadata"] = json.loads(item["metadata"] or "{}")
            parsed_messages.append(item)
        return {"conversation": dict(conversation), "messages": parsed_messages}

    def append_message(
        self,
        conversation_id: str,
        *,
        role: str,
        content: str,
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        created_at: float | None = None,
    ) -> str:
        if role not in {"user", "assistant", "tool"}:
            raise ValueError(f"Unsupported conversation role: {role}")
        text = content.strip()
        if not text:
            raise ValueError("Conversation messages cannot be empty")
        item_id = message_id or self._new_id()
        timestamp = created_at or time.time()
        with self._connect() as connection:
            conversation = connection.execute(
                "SELECT title FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if not conversation:
                raise KeyError(conversation_id)
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO messages(
                    id, conversation_id, role, content, metadata, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    conversation_id,
                    role,
                    text,
                    json.dumps(metadata or {}, separators=(",", ":")),
                    timestamp,
                ),
            )
            if cursor.rowcount:
                if role == "user" and conversation["title"] == "New conversation":
                    connection.execute(
                        "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                        (self._title_from_message(text), timestamp, conversation_id),
                    )
                else:
                    connection.execute(
                        "UPDATE conversations SET updated_at = ? WHERE id = ?",
                        (timestamp, conversation_id),
                    )
        return item_id

    def context_window(
        self,
        conversation_id: str,
        *,
        limit: int | None = None,
        max_chars: int | None = None,
    ) -> ContextWindow:
        """Return recent model-visible turns within message and character limits."""
        limit = (
            int(os.getenv("HISTORY_REHYDRATE_MESSAGES", str(DEFAULT_REHYDRATE_MESSAGES)))
            if limit is None
            else limit
        )
        max_chars = (
            int(os.getenv("HISTORY_REHYDRATE_MAX_CHARS", str(DEFAULT_REHYDRATE_MAX_CHARS)))
            if max_chars is None
            else max_chars
        )
        if limit <= 0 or max_chars <= 0:
            return ContextWindow(messages=[], truncated=False, oldest_rowid=None)

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT rowid, role, content
                FROM messages
                WHERE conversation_id = ? AND role IN ('user', 'assistant')
                ORDER BY rowid DESC
                LIMIT ?
                """,
                (conversation_id, limit),
            ).fetchall()
            total = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM messages
                WHERE conversation_id = ? AND role IN ('user', 'assistant')
                """,
                (conversation_id,),
            ).fetchone()["count"]

        selected: list[sqlite3.Row] = []
        character_count = 0
        for row in rows:
            content_length = len(row["content"])
            if selected and character_count + content_length > max_chars:
                break
            selected.append(row)
            character_count += content_length

        selected.reverse()
        messages = [
            {"role": str(row["role"]), "content": str(row["content"])}
            for row in selected
        ]
        return ContextWindow(
            messages=messages,
            truncated=len(selected) < total,
            oldest_rowid=int(selected[0]["rowid"]) if selected else None,
        )

    def context_messages(
        self,
        conversation_id: str,
        limit: int = DEFAULT_REHYDRATE_MESSAGES,
    ) -> list[dict[str, str]]:
        """Backward-compatible shortcut for callers that only need messages."""
        return self.context_window(conversation_id, limit=limit).messages

    def get_summary(self, conversation_id: str) -> tuple[str | None, int]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT summary, summary_through_rowid
                FROM conversations
                WHERE id = ?
                """,
                (conversation_id,),
            ).fetchone()
        if not row:
            raise KeyError(conversation_id)
        return row["summary"], int(row["summary_through_rowid"] or 0)

    def unsummarized_messages(
        self,
        conversation_id: str,
        *,
        after_rowid: int,
        before_rowid: int,
        max_chars: int,
    ) -> list[dict[str, Any]]:
        """Return the next chronological chunk eligible for summarization."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT rowid, role, content
                FROM messages
                WHERE conversation_id = ?
                  AND role IN ('user', 'assistant')
                  AND rowid > ?
                  AND rowid < ?
                ORDER BY rowid
                """,
                (conversation_id, after_rowid, before_rowid),
            ).fetchall()

        selected: list[dict[str, Any]] = []
        character_count = 0
        for row in rows:
            content_length = len(row["content"])
            if selected and character_count + content_length > max_chars:
                break
            selected.append(dict(row))
            character_count += content_length
        return selected

    def save_summary(
        self,
        conversation_id: str,
        summary: str,
        *,
        through_rowid: int,
    ) -> None:
        text = summary.strip()
        if not text:
            raise ValueError("Conversation summaries cannot be empty")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE conversations
                SET summary = ?, summary_through_rowid = ?
                WHERE id = ? AND summary_through_rowid <= ?
                """,
                (text, through_rowid, conversation_id, through_rowid),
            )
            if not cursor.rowcount and not connection.execute(
                "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone():
                raise KeyError(conversation_id)

    def delete(self, conversation_id: str) -> str:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM conversations WHERE id = ?", (conversation_id,)
            )
            if not cursor.rowcount:
                raise KeyError(conversation_id)
            replacement = connection.execute(
                "SELECT id FROM conversations ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
            active_id = (
                str(replacement["id"])
                if replacement
                else self._create_locked(connection, "New conversation")
            )
            connection.execute(
                "INSERT INTO conversation_state(key, value) VALUES ('active', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (active_id,),
            )
            return active_id
