"""Small SQLite-backed LangGraph Store used by the enterprise template.

This is a deterministic key-value store, not a vector store. It supplies the
``BaseStore`` contract required by DeepAgents' ``StoreBackend`` while keeping
semantic retrieval as a separate, explicit future integration.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langgraph.store.base import (
    BaseStore,
    GetOp,
    Item,
    ListNamespacesOp,
    MatchCondition,
    Op,
    PutOp,
    Result,
    SearchItem,
    SearchOp,
)

from agentseek_enterprise.relational import create_sqlalchemy_engine, require_sqlalchemy
from agentseek_enterprise.sqlite import (
    DEFAULT_SQLITE_BUSY_TIMEOUT_MS,
    DEFAULT_SQLITE_JOURNAL_MODE,
    configure_sqlite_connection,
    normalize_sqlite_journal_mode,
)


def build_langgraph_store(
    *,
    sqlalchemy_url: str = "",
    sqlite_path: str | Path = "./runtime/enterprise-long-term-store.sqlite3",
) -> BaseStore:
    if str(sqlalchemy_url or "").strip():
        return SQLAlchemyStore(str(sqlalchemy_url).strip())
    return SQLiteStore(sqlite_path)


class SQLiteStore(BaseStore):
    """Persistent LangGraph store with namespace and structured-filter support.

    It intentionally does not configure embeddings. ``SearchOp.query`` therefore
    performs deterministic namespace/filter lookup rather than semantic ranking.
    """

    __slots__ = ("_busy_timeout_ms", "_journal_mode", "_lock", "path")

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = DEFAULT_SQLITE_BUSY_TIMEOUT_MS,
        journal_mode: str = DEFAULT_SQLITE_JOURNAL_MODE,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self._busy_timeout_ms = max(0, int(busy_timeout_ms))
        self._journal_mode = normalize_sqlite_journal_mode(journal_mode)
        self._lock = threading.RLock()
        self._initialize()

    def batch(self, ops: Iterable[Op]) -> list[Result]:
        operation_list = list(ops)
        with self._lock, self._connect() as connection:
            results = [self._apply_op(connection, op) for op in operation_list]
        return results

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        return await asyncio.to_thread(self.batch, list(ops))

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS langgraph_store_items (
                    namespace_json TEXT NOT NULL,
                    item_key TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (namespace_json, item_key)
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=self._busy_timeout_ms / 1000)
        connection.row_factory = sqlite3.Row
        return configure_sqlite_connection(
            connection,
            busy_timeout_ms=self._busy_timeout_ms,
            journal_mode=self._journal_mode,
        )

    def _apply_op(self, connection: sqlite3.Connection, op: Op) -> Result:
        if isinstance(op, GetOp):
            return self._get_item(connection, op.namespace, op.key)
        if isinstance(op, PutOp):
            self._put_item(connection, op)
            return None
        if isinstance(op, SearchOp):
            return self._search_items(connection, op)
        if isinstance(op, ListNamespacesOp):
            return self._list_namespaces(connection, op)
        raise TypeError(f"Unsupported store operation: {type(op).__name__}")

    def _get_item(
        self,
        connection: sqlite3.Connection,
        namespace: tuple[str, ...],
        key: str,
    ) -> Item | None:
        row = connection.execute(
            """
            SELECT namespace_json, item_key, value_json, created_at, updated_at
            FROM langgraph_store_items
            WHERE namespace_json = ? AND item_key = ?
            """,
            (_namespace_json(namespace), str(key)),
        ).fetchone()
        return _item_from_row(row) if row is not None else None

    def _put_item(self, connection: sqlite3.Connection, op: PutOp) -> None:
        namespace = _namespace_json(op.namespace)
        key = str(op.key)
        if op.value is None:
            connection.execute(
                "DELETE FROM langgraph_store_items WHERE namespace_json = ? AND item_key = ?",
                (namespace, key),
            )
            return

        now = _utc_now()
        connection.execute(
            """
            INSERT INTO langgraph_store_items(namespace_json, item_key, value_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(namespace_json, item_key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            (namespace, key, json.dumps(op.value, ensure_ascii=False, separators=(",", ":")), now, now),
        )

    def _search_items(self, connection: sqlite3.Connection, op: SearchOp) -> list[SearchItem]:
        rows = connection.execute(
            "SELECT namespace_json, item_key, value_json, created_at, updated_at FROM langgraph_store_items"
        ).fetchall()
        matches: list[SearchItem] = []
        for row in rows:
            item = _item_from_row(row)
            if item.namespace[: len(op.namespace_prefix)] != op.namespace_prefix:
                continue
            if not _matches_filter(item.value, op.filter):
                continue
            matches.append(
                SearchItem(
                    namespace=item.namespace,
                    key=item.key,
                    value=item.value,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                    score=None,
                )
            )
        matches.sort(key=lambda item: (item.namespace, item.key))
        return matches[op.offset : op.offset + op.limit]

    def _list_namespaces(self, connection: sqlite3.Connection, op: ListNamespacesOp) -> list[tuple[str, ...]]:
        rows = connection.execute("SELECT DISTINCT namespace_json FROM langgraph_store_items").fetchall()
        namespaces = [_namespace_from_json(str(row["namespace_json"])) for row in rows]
        if op.match_conditions:
            namespaces = [
                namespace
                for namespace in namespaces
                if all(_matches_namespace(condition, namespace) for condition in op.match_conditions)
            ]
        if op.max_depth is not None:
            namespaces = list({namespace[: op.max_depth] for namespace in namespaces})
        return sorted(namespaces)[op.offset : op.offset + op.limit]


class SQLAlchemyStore(BaseStore):
    """Persistent LangGraph store backed by a SQLAlchemy database URL."""

    __slots__ = ("_engine", "_items", "_metadata", "_sa")

    def __init__(self, url: str) -> None:
        self._sa = require_sqlalchemy()
        self._engine = create_sqlalchemy_engine(url)
        self._metadata = self._sa.MetaData()
        self._items = self._sa.Table(
            "langgraph_store_items",
            self._metadata,
            self._sa.Column("namespace_json", self._sa.String(1024), primary_key=True),
            self._sa.Column("item_key", self._sa.String(512), primary_key=True),
            self._sa.Column("value_json", self._sa.Text, nullable=False),
            self._sa.Column("created_at", self._sa.String(64), nullable=False),
            self._sa.Column("updated_at", self._sa.String(64), nullable=False),
        )
        self._metadata.create_all(self._engine)

    def batch(self, ops: Iterable[Op]) -> list[Result]:
        operation_list = list(ops)
        with self._engine.begin() as connection:
            return [self._apply_op(connection, op) for op in operation_list]

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        return await asyncio.to_thread(self.batch, list(ops))

    def _apply_op(self, connection: Any, op: Op) -> Result:
        if isinstance(op, GetOp):
            return self._get_item(connection, op.namespace, op.key)
        if isinstance(op, PutOp):
            self._put_item(connection, op)
            return None
        if isinstance(op, SearchOp):
            return self._search_items(connection, op)
        if isinstance(op, ListNamespacesOp):
            return self._list_namespaces(connection, op)
        raise TypeError(f"Unsupported store operation: {type(op).__name__}")

    def _get_item(self, connection: Any, namespace: tuple[str, ...], key: str) -> Item | None:
        table = self._items
        row = (
            connection.execute(
                self._sa.select(table).where(
                    table.c.namespace_json == _namespace_json(namespace),
                    table.c.item_key == str(key),
                )
            )
            .mappings()
            .first()
        )
        return _item_from_mapping(row) if row is not None else None

    def _put_item(self, connection: Any, op: PutOp) -> None:
        table = self._items
        namespace = _namespace_json(op.namespace)
        key = str(op.key)
        connection.execute(table.delete().where(table.c.namespace_json == namespace, table.c.item_key == key))
        if op.value is None:
            return
        now = _utc_now()
        connection.execute(
            table.insert().values(
                namespace_json=namespace,
                item_key=key,
                value_json=json.dumps(op.value, ensure_ascii=False, separators=(",", ":")),
                created_at=now,
                updated_at=now,
            )
        )

    def _search_items(self, connection: Any, op: SearchOp) -> list[SearchItem]:
        rows = list(connection.execute(self._sa.select(self._items)).mappings())
        matches: list[SearchItem] = []
        for row in rows:
            item = _item_from_mapping(row)
            if item.namespace[: len(op.namespace_prefix)] != op.namespace_prefix:
                continue
            if not _matches_filter(item.value, op.filter):
                continue
            matches.append(
                SearchItem(
                    namespace=item.namespace,
                    key=item.key,
                    value=item.value,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                    score=None,
                )
            )
        matches.sort(key=lambda item: (item.namespace, item.key))
        return matches[op.offset : op.offset + op.limit]

    def _list_namespaces(self, connection: Any, op: ListNamespacesOp) -> list[tuple[str, ...]]:
        table = self._items
        rows = connection.execute(self._sa.select(table.c.namespace_json).distinct()).all()
        namespaces = [_namespace_from_json(str(row[0])) for row in rows]
        if op.match_conditions:
            namespaces = [
                namespace
                for namespace in namespaces
                if all(_matches_namespace(condition, namespace) for condition in op.match_conditions)
            ]
        if op.max_depth is not None:
            namespaces = list({namespace[: op.max_depth] for namespace in namespaces})
        return sorted(namespaces)[op.offset : op.offset + op.limit]


def _item_from_row(row: sqlite3.Row) -> Item:
    value = json.loads(str(row["value_json"]))
    if not isinstance(value, dict):
        raise TypeError("LangGraph store item value must be a JSON object.")
    return Item(
        namespace=_namespace_from_json(str(row["namespace_json"])),
        key=str(row["item_key"]),
        value=value,
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def _item_from_mapping(row: Mapping[str, Any]) -> Item:
    value = json.loads(str(row["value_json"]))
    if not isinstance(value, dict):
        raise TypeError("LangGraph store item value must be a JSON object.")
    return Item(
        namespace=_namespace_from_json(str(row["namespace_json"])),
        key=str(row["item_key"]),
        value=value,
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )


def _namespace_json(namespace: tuple[str, ...]) -> str:
    return json.dumps(list(namespace), ensure_ascii=True, separators=(",", ":"))


def _namespace_from_json(raw: str) -> tuple[str, ...]:
    values = json.loads(raw)
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError("LangGraph store namespace is invalid.")
    return tuple(values)


def _matches_filter(value: Mapping[str, Any], filter_values: Mapping[str, Any] | None) -> bool:
    if not filter_values:
        return True
    return all(_matches_filter_value(value.get(key), expected) for key, expected in filter_values.items())


def _matches_filter_value(actual: Any, expected: Any) -> bool:
    if not isinstance(expected, Mapping):
        return actual == expected
    for operator, value in expected.items():
        if operator == "$eq" and actual != value:
            return False
        if operator == "$ne" and actual == value:
            return False
        if operator == "$gt" and not _compare(actual, value, lambda left, right: left > right):
            return False
        if operator == "$gte" and not _compare(actual, value, lambda left, right: left >= right):
            return False
        if operator == "$lt" and not _compare(actual, value, lambda left, right: left < right):
            return False
        if operator == "$lte" and not _compare(actual, value, lambda left, right: left <= right):
            return False
        if operator not in {"$eq", "$ne", "$gt", "$gte", "$lt", "$lte"}:
            return False
    return True


def _compare(actual: Any, expected: Any, operator: Any) -> bool:
    try:
        return bool(operator(actual, expected))
    except TypeError:
        return False


def _matches_namespace(condition: MatchCondition, namespace: tuple[str, ...]) -> bool:
    path = condition.path
    if len(path) > len(namespace):
        return False
    candidate = namespace[: len(path)] if condition.match_type == "prefix" else namespace[-len(path) :]
    return all(expected == "*" or expected == actual for expected, actual in zip(path, candidate, strict=True))


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
