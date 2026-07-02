from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from agentseek_contextseek.pgvector import (
    DEFAULT_PGVECTOR_TABLE,
    PgVectorContextSeek,
    PgVectorSettings,
    _psycopg_url,
    _quote_table,
    get_default_pgvector_embedder,
)
from agentseek_contextseek.plugin import ContextSeekPlugin


@dataclass
class FakeEmbedder:
    vectors: dict[str, list[float]]
    dims: int = 3

    def embed(self, text: str) -> list[float]:
        return self.vectors.get(text, [0.0, 0.0, 1.0])


class FakePgVectorDatabase:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.next_id = 1

    def connect(self) -> FakeConnection:
        return FakeConnection(self)


class FakeConnection:
    def __init__(self, database: FakePgVectorDatabase) -> None:
        self.database = database

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.database)

    def commit(self) -> None:
        return None


class FakeCursor:
    def __init__(self, database: FakePgVectorDatabase) -> None:
        self.database = database
        self._one: dict[str, Any] | None = None
        self._many: list[dict[str, Any]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, query: str, params: dict[str, Any] | None = None) -> None:
        normalized = " ".join(query.lower().split())
        params = params or {}
        if normalized.startswith("insert into"):
            row = {
                "id": self.database.next_id,
                "scope": params["scope"],
                "content": params["content"],
                "embedding": _parse_vector(params["embedding"]),
                "source": params.get("source"),
                "source_type": params.get("source_type"),
                "tags": params.get("tags"),
            }
            self.database.next_id += 1
            self.database.rows.append(row)
            self._one = {"id": row["id"]}
            return
        if normalized.startswith("select"):
            query_vector = _parse_vector(params["embedding"])
            scope = params["scope"]
            limit = int(params["limit"])
            rows = [row for row in self.database.rows if row["scope"] == scope]
            rows.sort(key=lambda row: _cosine_distance(row["embedding"], query_vector))
            self._many = [
                {
                    "id": row["id"],
                    "scope": row["scope"],
                    "content": row["content"],
                    "source": row["source"],
                    "source_type": row["source_type"],
                    "tags": row["tags"],
                    "score": 1.0 - _cosine_distance(row["embedding"], query_vector),
                }
                for row in rows[:limit]
            ]

    def fetchone(self) -> dict[str, Any] | None:
        return self._one

    def fetchall(self) -> list[dict[str, Any]]:
        return self._many


def test_pgvector_add_retrieve_roundtrip():
    database = FakePgVectorDatabase()
    client = _fake_client(database)

    client.add("alpha memory", scope="employee/a", source="test://alpha", source_type="agent_inference", tags=["t"])
    result = client.retrieve("alpha query", scope="employee/a", k=5)

    assert len(result) == 1
    hit = result[0]
    assert hit.item.content_text == "alpha memory"
    assert hit.item.summary == "alpha memory"
    assert hit.item.scope == "employee/a"
    assert hit.item.tags == ["t"]
    assert hit.recall_path == "pgvector"


def test_pgvector_scope_isolation():
    database = FakePgVectorDatabase()
    client = _fake_client(database)
    client.add("alpha memory", scope="employee/a", source="test://a")
    client.add("beta memory", scope="employee/b", source="test://b")

    result = client.retrieve("alpha query", scope="employee/a", k=5)

    assert [hit.item.content_text for hit in result] == ["alpha memory"]


def test_pgvector_k_limit_and_cosine_ordering():
    database = FakePgVectorDatabase()
    client = _fake_client(database)
    client.add("beta memory", scope="employee/a", source="test://b")
    client.add("alpha memory", scope="employee/a", source="test://a")

    result = client.retrieve("alpha query", scope="employee/a", k=1)

    assert [hit.item.content_text for hit in result] == ["alpha memory"]


def test_default_embedder_is_singleton(monkeypatch):
    created: list[PgVectorSettings] = []

    class StubEmbedder:
        dims = 3

        def __init__(self, **kwargs: Any) -> None:
            created.append(kwargs["model_path"])

    monkeypatch.setattr("agentseek_contextseek.pgvector.BgeM3OnnxEmbedder", StubEmbedder)
    get_default_pgvector_embedder.cache_clear()
    settings = PgVectorSettings(
        url="postgresql://localhost/agentseek",
        dims=3,
        onnx_model_path="/models/bge-m3/model.onnx",
        tokenizer_path="/models/bge-m3/tokenizer.json",
    )

    first = get_default_pgvector_embedder(settings)
    second = get_default_pgvector_embedder(settings)

    assert first is second
    assert created == ["/models/bge-m3/model.onnx"]


def test_plugin_uses_pgvector_client_when_backend_selected(monkeypatch):
    monkeypatch.setenv("AGENTSEEK_CTX_STORAGE_BACKEND", "pgvector")
    client = MagicMock()
    with patch("agentseek_contextseek.plugin.PgVectorContextSeek") as mock_cls:
        mock_cls.from_env.return_value = client
        plugin = ContextSeekPlugin()
        assert plugin._get_client() is client
    client.initialize.assert_called_once()


def test_quote_table_rejects_unsafe_names():
    assert _quote_table(DEFAULT_PGVECTOR_TABLE) == '"contextseek_pgvector_items"'
    assert _quote_table("public.contextseek_pgvector_items") == '"public"."contextseek_pgvector_items"'
    with pytest.raises(ValueError):
        _quote_table("contextseek_pgvector_items; drop table users")


def test_sqlalchemy_postgres_url_is_accepted_for_psycopg():
    assert _psycopg_url("postgresql+psycopg://u:p@localhost/db") == "postgresql://u:p@localhost/db"


@pytest.mark.skipif(
    not os.environ.get("AGENTSEEK_CTX_PGVECTOR_TEST_URL"),
    reason="set AGENTSEEK_CTX_PGVECTOR_TEST_URL to run the real pgvector integration test",
)
def test_pgvector_real_postgres_roundtrip():
    table = "contextseek_pgvector_test_items"
    settings = PgVectorSettings(
        url=os.environ["AGENTSEEK_CTX_PGVECTOR_TEST_URL"],
        table=table,
        dims=3,
        create_hnsw_index=False,
    )
    database_scope = "pytest/pgvector/scope-a"
    client = PgVectorContextSeek(
        settings,
        embedder=FakeEmbedder(
            {
                "alpha memory": [1.0, 0.0, 0.0],
                "beta memory": [0.0, 1.0, 0.0],
                "alpha query": [1.0, 0.0, 0.0],
            }
        ),
    )
    client.initialize()
    _delete_real_rows(settings, database_scope)

    client.add("beta memory", scope=database_scope, source="pytest://beta")
    client.add("alpha memory", scope=database_scope, source="pytest://alpha")

    result = client.retrieve("alpha query", scope=database_scope, k=1)

    assert [hit.item.content_text for hit in result] == ["alpha memory"]
    _delete_real_rows(settings, database_scope)


def _fake_client(database: FakePgVectorDatabase) -> PgVectorContextSeek:
    settings = PgVectorSettings(
        url="",
        table="contextseek_pgvector_items",
        dims=3,
        create_hnsw_index=False,
    )
    return PgVectorContextSeek(
        settings,
        embedder=FakeEmbedder(
            {
                "alpha memory": [1.0, 0.0, 0.0],
                "beta memory": [0.0, 1.0, 0.0],
                "alpha query": [1.0, 0.0, 0.0],
            }
        ),
        connection_factory=database.connect,
    )


def _parse_vector(value: object) -> list[float]:
    text = str(value)
    return [float(part) for part in text.strip("[]").split(",") if part]


def _cosine_distance(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 1.0
    return 1.0 - (dot / (left_norm * right_norm))


def _delete_real_rows(settings: PgVectorSettings, scope: str) -> None:
    import psycopg

    with psycopg.connect(_psycopg_url(settings.url)) as connection, connection.cursor() as cursor:
        # The table name is validated by PgVectorSettings.quoted_table; scope is parameterized.
        cursor.execute(f"DELETE FROM {settings.quoted_table} WHERE scope = %s", (scope,))  # noqa: S608
        connection.commit()
