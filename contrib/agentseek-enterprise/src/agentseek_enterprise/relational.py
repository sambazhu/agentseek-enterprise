from __future__ import annotations

from pathlib import Path
from typing import Any

from agentseek_enterprise.sqlite import (
    DEFAULT_SQLITE_BUSY_TIMEOUT_MS,
    DEFAULT_SQLITE_JOURNAL_MODE,
    normalize_sqlite_journal_mode,
)


def require_sqlalchemy() -> Any:
    try:
        import sqlalchemy as sa
    except ImportError as exc:  # pragma: no cover - exercised only in minimal installs
        raise RuntimeError(
            "SQLAlchemy is required for enterprise relational memory stores. "
            "Install agentseek-enterprise with SQLAlchemy, then add a DB driver such as "
            "psycopg[binary] for PostgreSQL or pymysql for MySQL."
        ) from exc
    return sa


def create_sqlalchemy_engine(
    url: str,
    *,
    sqlite_busy_timeout_ms: int = DEFAULT_SQLITE_BUSY_TIMEOUT_MS,
    sqlite_journal_mode: str = DEFAULT_SQLITE_JOURNAL_MODE,
) -> Any:
    text = str(url or "").strip()
    if not text:
        raise ValueError("SQLAlchemy URL is required.")

    sa = require_sqlalchemy()
    parsed = sa.engine.make_url(text)
    connect_args: dict[str, Any] = {}
    engine_kwargs: dict[str, Any] = {
        "future": True,
        "pool_pre_ping": True,
    }

    if parsed.get_backend_name() == "sqlite":
        connect_args["check_same_thread"] = False
        _ensure_sqlite_parent(parsed.database)
    if connect_args:
        engine_kwargs["connect_args"] = connect_args

    engine = sa.create_engine(text, **engine_kwargs)
    if parsed.get_backend_name() == "sqlite":
        _configure_sqlalchemy_sqlite_engine(
            engine,
            busy_timeout_ms=sqlite_busy_timeout_ms,
            journal_mode=sqlite_journal_mode,
        )
    return engine


def _ensure_sqlite_parent(database: str | None) -> None:
    if not database or database == ":memory:":
        return
    path = Path(database).expanduser()
    if path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)


def _configure_sqlalchemy_sqlite_engine(
    engine: Any,
    *,
    busy_timeout_ms: int,
    journal_mode: str,
) -> None:
    sa = require_sqlalchemy()
    timeout = max(0, int(busy_timeout_ms))
    mode = normalize_sqlite_journal_mode(journal_mode)

    @sa.event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"PRAGMA busy_timeout = {timeout}")
            cursor.execute(f"PRAGMA journal_mode = {mode}")
        finally:
            cursor.close()
