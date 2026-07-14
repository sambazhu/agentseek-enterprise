from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

from agentseek_contextseek.pgvector import BgeM3OnnxEmbedder
from agentseek_enterprise.observability import elapsed_ms, emit_enterprise_event, event_timer

from enterprise_wecom_digital_employee.department_knowledge.models import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeDocumentSummary,
    KnowledgeSearchHit,
    SearchMode,
)
from enterprise_wecom_digital_employee.department_knowledge.settings import DepartmentKnowledgeSettings

_RRF_K = 60
_MAX_RESULTS = 20
_MAX_READ_CHUNKS = 20
_EXCERPT_CHARS = 600


class DepartmentKnowledgeRepository:
    """PostgreSQL implementation of the department-knowledge MCP contract.

    Tenant and collection scope come exclusively from server settings. MCP tool
    callers cannot override them, which keeps this local simulator aligned with
    the future department knowledge platform boundary.
    """

    def __init__(
        self,
        settings: DepartmentKnowledgeSettings,
        *,
        embedder: Any | None = None,
        connection_factory: Callable[[], Any] | None = None,
    ) -> None:
        if not settings.postgres_url and connection_factory is None:
            raise ValueError("AGENTSEEK_DEPARTMENT_KNOWLEDGE_POSTGRES_URL is required")
        self.settings = settings
        self._embedder = embedder
        self._connection_factory = connection_factory
        self._initialized = False
        self._init_lock = threading.Lock()

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            documents = _quote_identifier(self.settings.documents_table)
            chunks = _quote_identifier(self.settings.chunks_table)
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT extname FROM pg_extension WHERE extname IN ('vector', 'pg_trgm')"
                )
                extensions = {str(_row_value(row, "extname", 0)) for row in cursor.fetchall()}
                missing = {"vector", "pg_trgm"} - extensions
                if missing:
                    names = ", ".join(sorted(missing))
                    raise RuntimeError(f"department knowledge requires PostgreSQL extensions: {names}")
                cursor.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {documents} (
                        tenant_id text NOT NULL,
                        collection_id text NOT NULL,
                        document_id text NOT NULL,
                        title text NOT NULL,
                        source_name text NOT NULL,
                        source_sha256 text NOT NULL,
                        confidentiality_level text NOT NULL,
                        metadata jsonb NOT NULL DEFAULT jsonb_build_object(),
                        chunk_count integer NOT NULL DEFAULT 0,
                        created_at timestamptz NOT NULL DEFAULT now(),
                        updated_at timestamptz NOT NULL DEFAULT now(),
                        PRIMARY KEY (tenant_id, collection_id, document_id)
                    )
                    """
                )
                cursor.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {chunks} (
                        tenant_id text NOT NULL,
                        collection_id text NOT NULL,
                        document_id text NOT NULL,
                        chunk_id text NOT NULL,
                        ordinal integer NOT NULL,
                        heading text NOT NULL DEFAULT '',
                        content text NOT NULL,
                        embedding vector({self.settings.dims}) NOT NULL,
                        created_at timestamptz NOT NULL DEFAULT now(),
                        PRIMARY KEY (tenant_id, collection_id, chunk_id),
                        FOREIGN KEY (tenant_id, collection_id, document_id)
                            REFERENCES {documents} (tenant_id, collection_id, document_id)
                            ON DELETE CASCADE
                    )
                    """
                )
                cursor.execute(
                    f"CREATE INDEX IF NOT EXISTS {_index_name(self.settings.chunks_table, 'document')} "
                    f"ON {chunks} (tenant_id, collection_id, document_id, ordinal)"
                )
                cursor.execute(
                    f"CREATE INDEX IF NOT EXISTS {_index_name(self.settings.chunks_table, 'content_trgm')} "
                    f"ON {chunks} USING gin (content gin_trgm_ops)"
                )
                if self.settings.create_hnsw_index:
                    cursor.execute(
                        f"CREATE INDEX IF NOT EXISTS {_index_name(self.settings.chunks_table, 'embedding_hnsw')} "
                        f"ON {chunks} USING hnsw (embedding vector_cosine_ops)"
                    )
                connection.commit()
            self._initialized = True

    def upsert_document(
        self,
        document: KnowledgeDocument,
        chunks: Sequence[KnowledgeChunk],
    ) -> KnowledgeDocumentSummary:
        started_at = event_timer()
        if not chunks or any(chunk.document_id != document.document_id for chunk in chunks):
            raise ValueError("document import requires non-empty chunks for the same document")
        self.initialize()
        embeddings = [self._get_embedder().embed(chunk.content) for chunk in chunks]
        documents_table = _quote_identifier(self.settings.documents_table)
        chunks_table = _quote_identifier(self.settings.chunks_table)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%(lock_key)s, 0))",
                {
                    "lock_key": (
                        f"{self.settings.tenant_id}/{self.settings.collection_id}/{document.document_id}"
                    )
                },
            )
            cursor.execute(
                f"""
                INSERT INTO {documents_table} (
                    tenant_id, collection_id, document_id, title, source_name,
                    source_sha256, confidentiality_level, metadata, chunk_count
                ) VALUES (
                    %(tenant_id)s, %(collection_id)s, %(document_id)s, %(title)s,
                    %(source_name)s, %(source_sha256)s, %(confidentiality_level)s,
                    %(metadata)s, %(chunk_count)s
                )
                ON CONFLICT (tenant_id, collection_id, document_id) DO UPDATE SET
                    title = EXCLUDED.title,
                    source_name = EXCLUDED.source_name,
                    source_sha256 = EXCLUDED.source_sha256,
                    confidentiality_level = EXCLUDED.confidentiality_level,
                    metadata = EXCLUDED.metadata,
                    chunk_count = EXCLUDED.chunk_count,
                    updated_at = now()
                """,  # noqa: S608
                {
                    **self._scope_params(),
                    "document_id": document.document_id,
                    "title": document.title,
                    "source_name": document.source_name,
                    "source_sha256": document.source_sha256,
                    "confidentiality_level": document.confidentiality_level,
                    "metadata": _jsonb(document.metadata),
                    "chunk_count": len(chunks),
                },
            )
            cursor.execute(
                f"""
                DELETE FROM {chunks_table}
                WHERE tenant_id = %(tenant_id)s
                  AND collection_id = %(collection_id)s
                  AND document_id = %(document_id)s
                """,  # noqa: S608
                {**self._scope_params(), "document_id": document.document_id},
            )
            insert_sql = f"""
                INSERT INTO {chunks_table} (
                    tenant_id, collection_id, document_id, chunk_id,
                    ordinal, heading, content, embedding
                ) VALUES (
                    %(tenant_id)s, %(collection_id)s, %(document_id)s, %(chunk_id)s,
                    %(ordinal)s, %(heading)s, %(content)s, %(embedding)s::vector
                )
                """  # noqa: S608
            for chunk, embedding in zip(chunks, embeddings, strict=True):
                cursor.execute(
                    insert_sql,
                    {
                        **self._scope_params(),
                        "document_id": document.document_id,
                        "chunk_id": chunk.chunk_id,
                        "ordinal": chunk.ordinal,
                        "heading": chunk.heading,
                        "content": chunk.content,
                        "embedding": _vector_literal(embedding, self.settings.dims),
                    },
                )
            connection.commit()
        emit_enterprise_event(
            "department_knowledge_import",
            status="succeeded",
            collection_id=self.settings.collection_id,
            document_id=document.document_id,
            chunk_count=len(chunks),
            source_chars=len(document.text),
            duration_ms=elapsed_ms(started_at),
        )
        return _summary(document, len(chunks))

    def list_documents(self, *, limit: int = 20) -> tuple[KnowledgeDocumentSummary, ...]:
        self.initialize()
        documents = _quote_identifier(self.settings.documents_table)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT document_id, title, source_name, source_sha256,
                       confidentiality_level, chunk_count
                FROM {documents}
                WHERE tenant_id = %(tenant_id)s AND collection_id = %(collection_id)s
                ORDER BY updated_at DESC, document_id
                LIMIT %(limit)s
                """,  # noqa: S608
                {**self._scope_params(), "limit": _bounded_limit(limit)},
            )
            rows = cursor.fetchall()
        return tuple(_summary_from_row(row) for row in rows)

    def search(
        self,
        query: str,
        *,
        mode: SearchMode | str = SearchMode.HYBRID,
        limit: int = 8,
    ) -> tuple[KnowledgeSearchHit, ...]:
        clean_query = str(query or "").strip()
        if not clean_query:
            raise ValueError("department knowledge query must not be blank")
        search_mode = SearchMode(mode)
        bounded = _bounded_limit(limit)
        started_at = event_timer()
        if search_mode is SearchMode.KEYWORD:
            hits = self._search_keyword(clean_query, bounded)
        elif search_mode is SearchMode.SEMANTIC:
            hits = self._search_semantic(clean_query, bounded)
        else:
            candidate_limit = min(_MAX_RESULTS, max(12, bounded * 3))
            hits = _reciprocal_rank_fusion(
                self._search_keyword(clean_query, candidate_limit),
                self._search_semantic(clean_query, candidate_limit),
                limit=bounded,
            )
        emit_enterprise_event(
            "department_knowledge_search",
            status="succeeded",
            collection_id=self.settings.collection_id,
            mode=search_mode.value,
            query_chars=len(clean_query),
            hit_count=len(hits),
            duration_ms=elapsed_ms(started_at),
        )
        return hits

    def read_chunks(self, chunk_ids: Sequence[str]) -> tuple[dict[str, Any], ...]:
        selected = tuple(dict.fromkeys(str(value).strip() for value in chunk_ids if str(value).strip()))
        if not selected:
            return ()
        if len(selected) > _MAX_READ_CHUNKS:
            raise ValueError(f"at most {_MAX_READ_CHUNKS} chunks can be read at once")
        self.initialize()
        documents = _quote_identifier(self.settings.documents_table)
        chunks = _quote_identifier(self.settings.chunks_table)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT c.chunk_id, c.document_id, d.title, c.heading, c.ordinal, c.content
                FROM {chunks} c
                JOIN {documents} d
                  ON d.tenant_id = c.tenant_id
                 AND d.collection_id = c.collection_id
                 AND d.document_id = c.document_id
                WHERE c.tenant_id = %(tenant_id)s
                  AND c.collection_id = %(collection_id)s
                  AND c.chunk_id = ANY(%(chunk_ids)s)
                """,  # noqa: S608
                {**self._scope_params(), "chunk_ids": list(selected)},
            )
            rows = cursor.fetchall()
        by_id = {str(_row_value(row, "chunk_id", 0)): row for row in rows}
        return tuple(_chunk_dict(by_id[chunk_id]) for chunk_id in selected if chunk_id in by_id)

    def _search_keyword(self, query: str, limit: int) -> tuple[KnowledgeSearchHit, ...]:
        self.initialize()
        documents = _quote_identifier(self.settings.documents_table)
        chunks = _quote_identifier(self.settings.chunks_table)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT c.document_id, c.chunk_id, d.title, c.heading, c.content,
                       CASE
                         WHEN position(lower(%(query)s) in lower(c.content)) > 0 THEN 1.0
                         ELSE similarity(c.content, %(query)s)
                       END AS keyword_score
                FROM {chunks} c
                JOIN {documents} d
                  ON d.tenant_id = c.tenant_id
                 AND d.collection_id = c.collection_id
                 AND d.document_id = c.document_id
                WHERE c.tenant_id = %(tenant_id)s
                  AND c.collection_id = %(collection_id)s
                  AND (
                    position(lower(%(query)s) in lower(c.content)) > 0
                    OR similarity(c.content, %(query)s) >= %(threshold)s
                  )
                ORDER BY keyword_score DESC, c.document_id, c.ordinal
                LIMIT %(limit)s
                """,  # noqa: S608
                {
                    **self._scope_params(),
                    "query": query,
                    "threshold": self.settings.keyword_threshold,
                    "limit": limit,
                },
            )
            rows = cursor.fetchall()
        return tuple(_hit_from_row(row, score_key="keyword_score") for row in rows)

    def _search_semantic(self, query: str, limit: int) -> tuple[KnowledgeSearchHit, ...]:
        self.initialize()
        documents = _quote_identifier(self.settings.documents_table)
        chunks = _quote_identifier(self.settings.chunks_table)
        embedding = _vector_literal(self._get_embedder().embed(query), self.settings.dims)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT c.document_id, c.chunk_id, d.title, c.heading, c.content,
                       1 - (c.embedding <=> %(embedding)s::vector) AS semantic_score
                FROM {chunks} c
                JOIN {documents} d
                  ON d.tenant_id = c.tenant_id
                 AND d.collection_id = c.collection_id
                 AND d.document_id = c.document_id
                WHERE c.tenant_id = %(tenant_id)s
                  AND c.collection_id = %(collection_id)s
                ORDER BY c.embedding <=> %(embedding)s::vector, c.document_id, c.ordinal
                LIMIT %(limit)s
                """,  # noqa: S608
                {**self._scope_params(), "embedding": embedding, "limit": limit},
            )
            rows = cursor.fetchall()
        return tuple(_hit_from_row(row, score_key="semantic_score") for row in rows)

    def _scope_params(self) -> dict[str, str]:
        return {
            "tenant_id": self.settings.tenant_id,
            "collection_id": self.settings.collection_id,
        }

    def _get_embedder(self) -> Any:
        if self._embedder is None:
            if not self.settings.onnx_model_path or not self.settings.tokenizer_path:
                raise RuntimeError("department knowledge semantic search requires bge-m3 ONNX paths")
            self._embedder = BgeM3OnnxEmbedder(
                model_path=self.settings.onnx_model_path,
                tokenizer_path=self.settings.tokenizer_path,
                dims=self.settings.dims,
                max_length=self.settings.max_length,
            )
        if int(self._embedder.dims) != self.settings.dims:
            raise ValueError("department knowledge embedder dimensions do not match settings")
        return self._embedder

    def _connect(self) -> Any:
        if self._connection_factory is not None:
            return self._connection_factory()
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise RuntimeError("psycopg is required for department knowledge") from exc
        connect = cast("Any", psycopg.connect)
        return connect(_psycopg_url(self.settings.postgres_url), row_factory=dict_row)


def _reciprocal_rank_fusion(
    keyword_hits: Sequence[KnowledgeSearchHit],
    semantic_hits: Sequence[KnowledgeSearchHit],
    *,
    limit: int,
) -> tuple[KnowledgeSearchHit, ...]:
    scores: dict[str, float] = {}
    hits: dict[str, KnowledgeSearchHit] = {}
    keyword_scores: dict[str, float] = {}
    semantic_scores: dict[str, float] = {}
    for ranked in (keyword_hits, semantic_hits):
        for rank, hit in enumerate(ranked, start=1):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (_RRF_K + rank)
            hits.setdefault(hit.chunk_id, hit)
            if hit.keyword_score is not None:
                keyword_scores[hit.chunk_id] = hit.keyword_score
            if hit.semantic_score is not None:
                semantic_scores[hit.chunk_id] = hit.semantic_score
    ordered = sorted(scores, key=lambda chunk_id: (-scores[chunk_id], chunk_id))[:limit]
    return tuple(
        KnowledgeSearchHit(
            document_id=hits[chunk_id].document_id,
            chunk_id=chunk_id,
            title=hits[chunk_id].title,
            heading=hits[chunk_id].heading,
            excerpt=hits[chunk_id].excerpt,
            score=scores[chunk_id],
            keyword_score=keyword_scores.get(chunk_id),
            semantic_score=semantic_scores.get(chunk_id),
        )
        for chunk_id in ordered
    )


def _summary(document: KnowledgeDocument, chunk_count: int) -> KnowledgeDocumentSummary:
    return KnowledgeDocumentSummary(
        document_id=document.document_id,
        title=document.title,
        source_name=document.source_name,
        source_sha256=document.source_sha256,
        confidentiality_level=document.confidentiality_level,
        chunk_count=chunk_count,
    )


def _summary_from_row(row: Any) -> KnowledgeDocumentSummary:
    return KnowledgeDocumentSummary(
        document_id=str(_row_value(row, "document_id", 0)),
        title=str(_row_value(row, "title", 1)),
        source_name=str(_row_value(row, "source_name", 2)),
        source_sha256=str(_row_value(row, "source_sha256", 3)),
        confidentiality_level=str(_row_value(row, "confidentiality_level", 4)),
        chunk_count=int(_row_value(row, "chunk_count", 5) or 0),
    )


def _hit_from_row(row: Any, *, score_key: str) -> KnowledgeSearchHit:
    score_index = 5
    score = float(_row_value(row, score_key, score_index) or 0.0)
    content = str(_row_value(row, "content", 4) or "")
    return KnowledgeSearchHit(
        document_id=str(_row_value(row, "document_id", 0)),
        chunk_id=str(_row_value(row, "chunk_id", 1)),
        title=str(_row_value(row, "title", 2)),
        heading=str(_row_value(row, "heading", 3)),
        excerpt=_excerpt(content),
        score=score,
        keyword_score=score if score_key == "keyword_score" else None,
        semantic_score=score if score_key == "semantic_score" else None,
    )


def _chunk_dict(row: Any) -> dict[str, Any]:
    return {
        "chunk_id": str(_row_value(row, "chunk_id", 0)),
        "document_id": str(_row_value(row, "document_id", 1)),
        "title": str(_row_value(row, "title", 2)),
        "heading": str(_row_value(row, "heading", 3)),
        "ordinal": int(_row_value(row, "ordinal", 4) or 0),
        "content": str(_row_value(row, "content", 5)),
    }


def _excerpt(content: str) -> str:
    compact = " ".join(content.split())
    return compact if len(compact) <= _EXCERPT_CHARS else f"{compact[:_EXCERPT_CHARS].rstrip()}..."


def _bounded_limit(limit: int) -> int:
    return max(1, min(int(limit), _MAX_RESULTS))


def _row_value(row: Any, key: str, index: int) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    if isinstance(row, Sequence) and not isinstance(row, (str, bytes)):
        return row[index] if index < len(row) else None
    return getattr(row, key, None)


def _quote_identifier(value: str) -> str:
    if not value or not value.replace("_", "a").isalnum() or value[0].isdigit():
        raise ValueError("invalid PostgreSQL identifier")
    return f'"{value}"'


def _index_name(table: str, suffix: str) -> str:
    return _quote_identifier(f"idx_{table}_{suffix}")


def _vector_literal(vector: Sequence[float], dims: int) -> str:
    if len(vector) != dims:
        raise ValueError(f"embedding dimensions must equal {dims}")
    return "[" + ",".join(f"{float(value):.12g}" for value in vector) + "]"


def _jsonb(value: Mapping[str, Any]) -> Any:
    try:
        from psycopg.types.json import Jsonb
    except ImportError:
        return json.dumps(dict(value), ensure_ascii=False)
    return Jsonb(dict(value))


def _psycopg_url(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("postgresql+psycopg://"):
        return "postgresql://" + text.removeprefix("postgresql+psycopg://")
    if text.startswith("postgres+psycopg://"):
        return "postgresql://" + text.removeprefix("postgres+psycopg://")
    return text
