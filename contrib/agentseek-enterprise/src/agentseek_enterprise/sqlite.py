from __future__ import annotations

import sqlite3

DEFAULT_SQLITE_BUSY_TIMEOUT_MS = 30_000
DEFAULT_SQLITE_JOURNAL_MODE = "WAL"

_ALLOWED_JOURNAL_MODES = {"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"}


def normalize_sqlite_journal_mode(value: str | None) -> str:
    mode = str(value or DEFAULT_SQLITE_JOURNAL_MODE).strip().upper()
    if not mode:
        return DEFAULT_SQLITE_JOURNAL_MODE
    if mode not in _ALLOWED_JOURNAL_MODES:
        raise ValueError(f"Unsupported SQLite journal mode: {value!r}")
    return mode


def configure_sqlite_connection(
    connection: sqlite3.Connection,
    *,
    busy_timeout_ms: int = DEFAULT_SQLITE_BUSY_TIMEOUT_MS,
    journal_mode: str = DEFAULT_SQLITE_JOURNAL_MODE,
) -> sqlite3.Connection:
    timeout = max(0, int(busy_timeout_ms))
    mode = normalize_sqlite_journal_mode(journal_mode)
    connection.execute(f"PRAGMA busy_timeout = {timeout}")
    connection.execute(f"PRAGMA journal_mode = {mode}")
    return connection
