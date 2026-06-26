from __future__ import annotations

import os
import shlex
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SHORT_TERM_MEMORY_STATE_KEY = "short_term_memory"


@dataclass(frozen=True)
class ShortTermMemorySettings:
    """Settings for per-session short-term conversation memory."""

    enabled: bool = False
    sqlite_path: Path = Path("./runtime/enterprise-short-term-memory.sqlite3")
    recent_turns: int = 8
    ttl_seconds: int = 7 * 24 * 60 * 60
    max_content_chars: int = 4000

    @classmethod
    def from_env(cls) -> "ShortTermMemorySettings":
        _load_dotenv_if_present(Path.cwd() / ".env")
        return cls(
            enabled=_truthy(os.environ.get("AGENTSEEK_ENTERPRISE_MEMORY_ENABLED")),
            sqlite_path=_sqlite_path_from_env(),
            recent_turns=max(1, int(os.environ.get("AGENTSEEK_ENTERPRISE_MEMORY_RECENT_TURNS", "8"))),
            ttl_seconds=max(0, int(os.environ.get("AGENTSEEK_ENTERPRISE_MEMORY_TTL_SECONDS", str(7 * 24 * 60 * 60)))),
            max_content_chars=max(1, int(os.environ.get("AGENTSEEK_ENTERPRISE_MEMORY_MAX_CONTENT_CHARS", "4000"))),
        )


class SQLiteShortTermMemoryStore:
    """Small SQLite-backed store for recent user/assistant turns."""

    def __init__(self, settings: ShortTermMemorySettings) -> None:
        self.settings = settings
        self.path = settings.sqlite_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def load_recent_messages(self, session_id: str) -> list[dict[str, Any]]:
        self.prune_expired()
        limit = self.settings.recent_turns * 2
        with closing(self._connect()) as connection:
            with connection:
                rows = connection.execute(
                    """
                    SELECT role, content, created_at
                    FROM (
                        SELECT role, content, created_at, id
                        FROM enterprise_short_term_messages
                        WHERE session_id = ?
                        ORDER BY id DESC
                        LIMIT ?
                    )
                    ORDER BY id ASC
                    """,
                    (session_id, limit),
                ).fetchall()
        return [
            {
                "role": row["role"],
                "content": row["content"],
                "created_at": int(row["created_at"]),
            }
            for row in rows
        ]

    def append_turn(self, session_id: str, user_content: str, assistant_content: str) -> None:
        user_content = _truncate(user_content, self.settings.max_content_chars)
        assistant_content = _truncate(assistant_content, self.settings.max_content_chars)
        if not user_content and not assistant_content:
            return

        now = int(time.time())
        with closing(self._connect()) as connection:
            with connection:
                self._prune_expired(connection, now)
                if user_content:
                    connection.execute(
                        """
                        INSERT INTO enterprise_short_term_messages (session_id, role, content, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (session_id, "user", user_content, now),
                    )
                if assistant_content:
                    connection.execute(
                        """
                        INSERT INTO enterprise_short_term_messages (session_id, role, content, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (session_id, "assistant", assistant_content, now),
                    )

    def prune_expired(self) -> None:
        if self.settings.ttl_seconds <= 0:
            return
        with closing(self._connect()) as connection:
            with connection:
                self._prune_expired(connection, int(time.time()))

    def _ensure_schema(self) -> None:
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS enterprise_short_term_messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                        content TEXT NOT NULL,
                        created_at INTEGER NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_enterprise_short_term_messages_session_id
                    ON enterprise_short_term_messages (session_id, id)
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_enterprise_short_term_messages_created_at
                    ON enterprise_short_term_messages (created_at)
                    """
                )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _prune_expired(self, connection: sqlite3.Connection, now: int) -> None:
        if self.settings.ttl_seconds <= 0:
            return
        cutoff = now - self.settings.ttl_seconds
        connection.execute("DELETE FROM enterprise_short_term_messages WHERE created_at < ?", (cutoff,))


def short_term_memory_enabled_from_env() -> bool:
    _load_dotenv_if_present(Path.cwd() / ".env")
    return _truthy(os.environ.get("AGENTSEEK_ENTERPRISE_MEMORY_ENABLED"))


def short_term_memory_state(session_id: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "recent_messages": messages,
        "message_count": len(messages),
        "source": "sqlite",
    }


def format_short_term_memory_for_prompt(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    messages = value.get("recent_messages")
    if not isinstance(messages, list) or not messages:
        return None

    lines = [
        "[ShortTermMemory]",
        "以下是同一员工同一会话的近期对话。用于理解追问、代词、继续处理和刚才提到的事项，不代表最终授权。",
    ]
    for item in messages:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        content = str(item.get("content") or "").strip()
        if not role or not content:
            continue
        label = "用户" if role == "user" else "助手" if role == "assistant" else role
        lines.append(f"{label}: {content}")
    return "\n".join(lines) if len(lines) > 2 else None


def _sqlite_path_from_env() -> Path:
    raw_path = (
        os.environ.get("AGENTSEEK_ENTERPRISE_MEMORY_SQLITE_PATH")
        or os.environ.get("AGENTSEEK_ENTERPRISE_SHORT_TERM_MEMORY_SQLITE_PATH")
        or "./runtime/enterprise-short-term-memory.sqlite3"
    )
    text = raw_path.strip()
    for prefix in ("sqlite+pysqlite:///", "sqlite:///"):
        if text.startswith(prefix):
            return Path(text.removeprefix(prefix)).expanduser()
    return Path(text).expanduser()


def _truncate(value: str, max_chars: int) -> str:
    text = value.strip()
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return text[:max_chars]
    return text[: max_chars - 3].rstrip() + "..."


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _load_dotenv_if_present(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        try:
            parsed = shlex.split(value, comments=False, posix=True)
            os.environ[key] = parsed[0] if parsed else ""
        except ValueError:
            os.environ[key] = value.strip().strip("'\"")
