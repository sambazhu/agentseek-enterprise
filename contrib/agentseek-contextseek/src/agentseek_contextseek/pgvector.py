from __future__ import annotations

import json
import math
import os
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from contextseek.domain.context_item import ContextItem
from contextseek.domain.provenance import Provenance
from contextseek.domain.results import RetrieveResponse, SearchHit
from contextseek.domain.stages import STAGE_CONFIDENCE, Stage

PGVECTOR_BACKEND = "pgvector"
DEFAULT_PGVECTOR_TABLE = "contextseek_pgvector_items"
DEFAULT_BGE_M3_DIMS = 1024

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class Embedder(Protocol):
    dims: int

    def embed(self, text: str) -> list[float]: ...


@dataclass(frozen=True)
class PgVectorSettings:
    """Settings for the AgentSeek-owned ContextSeek pgvector backend.

    The backend intentionally lives in ``agentseek-contextseek`` instead of
    patching the upstream ``contextseek`` package. SeekDB/OceanBase/memory keep
    their upstream behavior; this is an additive backend selected only when
    ``AGENTSEEK_CTX_STORAGE_BACKEND=pgvector``.
    """

    url: str
    table: str = DEFAULT_PGVECTOR_TABLE
    dims: int = DEFAULT_BGE_M3_DIMS
    onnx_model_path: str = ""
    tokenizer_path: str = ""
    max_length: int = 8192
    create_hnsw_index: bool = True

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> PgVectorSettings:
        env = os.environ if environ is None else environ
        return cls(
            url=(
                env.get("AGENTSEEK_CTX_PGVECTOR_URL")
                or env.get("AGENTSEEK_CTX_POSTGRES_URL")
                or env.get("PGVECTOR_URL")
                or ""
            ).strip(),
            table=(env.get("AGENTSEEK_CTX_PGVECTOR_TABLE") or DEFAULT_PGVECTOR_TABLE).strip(),
            dims=max(1, int(env.get("AGENTSEEK_CTX_PGVECTOR_DIMS", str(DEFAULT_BGE_M3_DIMS)))),
            onnx_model_path=(env.get("AGENTSEEK_CTX_BGE_M3_ONNX_MODEL_PATH") or "").strip(),
            tokenizer_path=(env.get("AGENTSEEK_CTX_BGE_M3_TOKENIZER_PATH") or "").strip(),
            max_length=max(1, int(env.get("AGENTSEEK_CTX_BGE_M3_MAX_LENGTH", "8192"))),
            create_hnsw_index=_truthy(env.get("AGENTSEEK_CTX_PGVECTOR_CREATE_HNSW_INDEX", "true")),
        )

    @property
    def quoted_table(self) -> str:
        return _quote_table(self.table)


class BgeM3OnnxEmbedder:
    """Dense BAAI/bge-m3 embedder backed by onnxruntime + tokenizers.

    This class deliberately avoids torch, FlagEmbedding, and
    sentence-transformers in the gateway process. It expects a previously
    exported ONNX model plus a Hugging Face ``tokenizer.json`` file from the
    bge-m3 model directory.
    """

    def __init__(
        self,
        *,
        model_path: str | Path,
        tokenizer_path: str | Path,
        dims: int = DEFAULT_BGE_M3_DIMS,
        max_length: int = 8192,
    ) -> None:
        self.dims = int(dims)
        self.max_length = int(max_length)
        self._session = _load_onnx_session(model_path)
        self._tokenizer = _load_tokenizer(tokenizer_path)
        self._input_names = {input_.name for input_ in self._session.get_inputs()}

    def embed(self, text: str) -> list[float]:
        import numpy as np

        encoded = self._tokenizer.encode(str(text or ""))
        input_ids = encoded.ids[: self.max_length]
        attention_mask = encoded.attention_mask[: self.max_length]
        type_ids = encoded.type_ids[: self.max_length]
        if not input_ids:
            input_ids = [0]
            attention_mask = [0]
            type_ids = [0]

        inputs: dict[str, Any] = {}
        if "input_ids" in self._input_names:
            inputs["input_ids"] = np.asarray([input_ids], dtype=np.int64)
        if "attention_mask" in self._input_names:
            inputs["attention_mask"] = np.asarray([attention_mask], dtype=np.int64)
        if "token_type_ids" in self._input_names:
            inputs["token_type_ids"] = np.asarray([type_ids], dtype=np.int64)

        outputs = self._session.run(None, inputs)
        vector = _pool_onnx_outputs(outputs, attention_mask)
        if len(vector) != self.dims:
            raise RuntimeError(f"bge-m3 ONNX embedding dims mismatch: expected {self.dims}, got {len(vector)}")
        return _l2_normalize(vector)


class PgVectorContextSeek:
    """Small ContextSeek-compatible client backed by PostgreSQL + pgvector."""

    def __init__(
        self,
        settings: PgVectorSettings,
        *,
        embedder: Embedder | None = None,
        connection_factory: Callable[[], Any] | None = None,
    ) -> None:
        if not settings.url and connection_factory is None:
            raise ValueError("AGENTSEEK_CTX_PGVECTOR_URL is required when STORAGE_BACKEND=pgvector.")
        self.settings = settings
        self.embedder = embedder or get_default_pgvector_embedder(settings)
        if self.embedder.dims != settings.dims:
            raise ValueError(f"pgvector dims mismatch: settings={settings.dims}, embedder={self.embedder.dims}")
        self._connection_factory = connection_factory
        self._initialized = False
        self._init_lock = threading.Lock()

    @classmethod
    def from_env(cls) -> PgVectorContextSeek:
        return cls(PgVectorSettings.from_env())

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            table = self.settings.quoted_table
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        id bigserial PRIMARY KEY,
                        scope text NOT NULL,
                        content text NOT NULL,
                        embedding vector({self.settings.dims}) NOT NULL,
                        source text,
                        source_type text,
                        tags jsonb,
                        created_at timestamptz DEFAULT now()
                    )
                    """
                )
                cursor.execute(
                    f"CREATE INDEX IF NOT EXISTS {_index_name(self.settings.table, 'scope')} ON {table} (scope)"
                )
                if self.settings.create_hnsw_index:
                    cursor.execute(
                        f"""
                        CREATE INDEX IF NOT EXISTS {_index_name(self.settings.table, 'embedding_hnsw')}
                        ON {table} USING hnsw (embedding vector_cosine_ops)
                        """
                    )
                connection.commit()
            self._initialized = True

    def add(
        self,
        content: str | Mapping[str, Any],
        *,
        scope: str,
        source: str,
        source_type: str = "human_input",
        tags: list[str] | None = None,
        **_: Any,
    ) -> ContextItem:
        self.initialize()
        content_text = _content_text(content)
        embedding = self.embedder.embed(content_text)
        vector = _vector_literal(embedding, self.settings.dims)
        table = self.settings.quoted_table
        tags_json = _jsonb(tags or [])
        # The table name is validated by _quote_table(); values stay parameterized.
        insert_sql = f"""
                INSERT INTO {table} (scope, content, embedding, source, source_type, tags)
                VALUES (%(scope)s, %(content)s, %(embedding)s::vector, %(source)s, %(source_type)s, %(tags)s)
                RETURNING id
                """  # noqa: S608
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                insert_sql,
                {
                    "scope": scope,
                    "content": content_text,
                    "embedding": vector,
                    "source": source,
                    "source_type": source_type,
                    "tags": tags_json,
                },
            )
            row = cursor.fetchone()
            connection.commit()

        item = _item_from_values(
            item_id=str(_row_value(row, "id") or ""),
            scope=scope,
            content=content_text,
            source=source,
            source_type=source_type,
            tags=tags or [],
        )
        item.embedding = embedding
        return item

    def retrieve(
        self,
        query: str,
        *,
        scope: str,
        k: int = 10,
        **_: Any,
    ) -> RetrieveResponse:
        self.initialize()
        query_embedding = self.embedder.embed(query)
        vector = _vector_literal(query_embedding, self.settings.dims)
        limit = max(1, int(k))
        table = self.settings.quoted_table
        # The table name is validated by _quote_table(); values stay parameterized.
        select_sql = f"""
                SELECT id, scope, content, source, source_type, tags, created_at,
                       1 - (embedding <=> %(embedding)s::vector) AS score
                FROM {table}
                WHERE scope = %(scope)s
                ORDER BY embedding <=> %(embedding)s::vector
                LIMIT %(limit)s
                """  # noqa: S608
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                select_sql,
                {"scope": scope, "embedding": vector, "limit": limit},
            )
            rows = cursor.fetchall()

        hits: list[SearchHit] = []
        for row in rows:
            item = _item_from_values(
                item_id=str(_row_value(row, "id") or ""),
                scope=str(_row_value(row, "scope") or scope),
                content=str(_row_value(row, "content") or ""),
                source=str(_row_value(row, "source") or ""),
                source_type=str(_row_value(row, "source_type") or "agent_inference"),
                tags=_tags_from_row(_row_value(row, "tags")),
            )
            score = float(_row_value(row, "score") or 0.0)
            hits.append(
                SearchHit(
                    item=item,
                    score=score,
                    layer="full",
                    provenance_summary=item.provenance.source_id,
                    stage_confidence=STAGE_CONFIDENCE.get(item.stage, 0.3),
                    recall_path="pgvector",
                )
            )
        return RetrieveResponse(items=hits)

    def _connect(self) -> Any:
        if self._connection_factory is not None:
            return self._connection_factory()
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - exercised in deployment only
            raise RuntimeError("psycopg is required for AGENTSEEK_CTX_STORAGE_BACKEND=pgvector.") from exc
        return psycopg.connect(_psycopg_url(self.settings.url), row_factory=dict_row)


@lru_cache(maxsize=8)
def get_default_pgvector_embedder(settings: PgVectorSettings) -> BgeM3OnnxEmbedder:
    if not settings.onnx_model_path or not settings.tokenizer_path:
        raise RuntimeError(
            "bge-m3 ONNX embedding requires AGENTSEEK_CTX_BGE_M3_ONNX_MODEL_PATH "
            "and AGENTSEEK_CTX_BGE_M3_TOKENIZER_PATH."
        )
    return BgeM3OnnxEmbedder(
        model_path=settings.onnx_model_path,
        tokenizer_path=settings.tokenizer_path,
        dims=settings.dims,
        max_length=settings.max_length,
    )


def _load_onnx_session(model_path: str | Path) -> Any:
    path = Path(model_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"bge-m3 ONNX model not found: {path}")
    try:
        import onnxruntime as ort
    except ImportError as exc:  # pragma: no cover - exercised in deployment only
        raise RuntimeError("onnxruntime is required for bge-m3 ONNX embeddings.") from exc
    return ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])


def _load_tokenizer(tokenizer_path: str | Path) -> Any:
    path = Path(tokenizer_path).expanduser()
    if path.is_dir():
        path = path / "tokenizer.json"
    if not path.is_file():
        raise FileNotFoundError(f"bge-m3 tokenizer.json not found: {path}")
    try:
        from tokenizers import Tokenizer
    except ImportError as exc:  # pragma: no cover - exercised in deployment only
        raise RuntimeError("tokenizers is required for bge-m3 ONNX embeddings.") from exc
    return Tokenizer.from_file(str(path))


def _pool_onnx_outputs(outputs: Sequence[Any], attention_mask: Sequence[int]) -> list[float]:
    import numpy as np

    for output in outputs:
        arr = np.asarray(output)
        if arr.ndim == 2:
            return [float(x) for x in arr[0].tolist()]
        if arr.ndim == 3:
            mask = np.asarray(attention_mask, dtype=np.float32)[None, :, None]
            hidden = arr.astype(np.float32)
            denom = max(float(mask.sum()), 1.0)
            pooled = (hidden * mask).sum(axis=1) / denom
            return [float(x) for x in pooled[0].tolist()]
    raise RuntimeError("bge-m3 ONNX model did not return a 2D or 3D embedding output.")


def _l2_normalize(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(float(x) * float(x) for x in vector))
    if norm <= 0:
        return [0.0 for _ in vector]
    return [float(x) / norm for x in vector]


def _quote_table(value: str) -> str:
    parts = [part.strip() for part in value.split(".")]
    if not parts or len(parts) > 2 or any(not _IDENTIFIER_RE.fullmatch(part) for part in parts):
        raise ValueError(f"Invalid pgvector table name: {value!r}")
    return ".".join(f'"{part}"' for part in parts)


def _index_name(table: str, suffix: str) -> str:
    raw = table.replace(".", "_")
    compact = re.sub(r"[^A-Za-z0-9_]", "_", raw)
    return _quote_table(f"idx_{compact}_{suffix}")


def _vector_literal(vector: Sequence[float], dims: int) -> str:
    if len(vector) != dims:
        raise ValueError(f"embedding dims mismatch: expected {dims}, got {len(vector)}")
    return "[" + ",".join(f"{float(value):.12g}" for value in vector) + "]"


def _psycopg_url(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("postgresql+psycopg://"):
        return "postgresql://" + text.removeprefix("postgresql+psycopg://")
    if text.startswith("postgres+psycopg://"):
        return "postgresql://" + text.removeprefix("postgres+psycopg://")
    return text


def _jsonb(value: Any) -> Any:
    try:
        from psycopg.types.json import Jsonb
    except ImportError:
        return json.dumps(value, ensure_ascii=False)
    return Jsonb(value)


def _content_text(content: str | Mapping[str, Any]) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, sort_keys=True)


def _item_from_values(
    *,
    item_id: str,
    scope: str,
    content: str,
    source: str,
    source_type: str,
    tags: list[str],
) -> ContextItem:
    kwargs: dict[str, Any] = {}
    if item_id:
        kwargs["id"] = item_id
    return ContextItem(
        content=content,
        scope=scope,
        provenance=Provenance(source_type=source_type or "agent_inference", source_id=source or "pgvector"),
        summary=content[:500],
        tags=tags,
        stage=Stage.raw,
        **kwargs,
    )


def _row_value(row: Any, key: str) -> Any:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return row.get(key)
    try:
        return getattr(row, key)
    except AttributeError:
        pass
    if isinstance(row, Sequence) and not isinstance(row, str):
        keys = ("id", "scope", "content", "source", "source_type", "tags", "created_at", "score")
        try:
            return row[keys.index(key)]
        except (ValueError, IndexError):
            return None
    return None


def _tags_from_row(value: Any) -> list[str]:
    if value is None:
        return []
    wrapped = getattr(value, "obj", None)
    if wrapped is not None:
        return _tags_from_row(wrapped)
    wrapped = getattr(value, "wrapped", None)
    if wrapped is not None:
        return _tags_from_row(wrapped)
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value]
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    return []


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}
