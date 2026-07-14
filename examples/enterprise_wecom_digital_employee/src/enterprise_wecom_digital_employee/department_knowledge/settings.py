from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_DIMS = 1024
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class DepartmentKnowledgeSettings:
    postgres_url: str
    tenant_id: str = "strategic-development"
    collection_id: str = "strategic-development"
    owning_org: str = "战略发展部"
    table_prefix: str = "department_knowledge"
    dims: int = DEFAULT_DIMS
    onnx_model_path: str = ""
    tokenizer_path: str = ""
    max_length: int = 8192
    chunk_chars: int = 1200
    chunk_overlap_chars: int = 150
    keyword_threshold: float = 0.08
    create_hnsw_index: bool = True

    def __post_init__(self) -> None:
        for field_name in ("tenant_id", "collection_id", "owning_org", "table_prefix"):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"{field_name} must not be blank")
        if not _IDENTIFIER_RE.fullmatch(self.table_prefix):
            raise ValueError("table_prefix must be a safe PostgreSQL identifier")
        if self.dims <= 0 or self.max_length <= 0 or self.chunk_chars <= 0:
            raise ValueError("knowledge dimensions and size limits must be positive")
        if self.chunk_overlap_chars < 0 or self.chunk_overlap_chars >= self.chunk_chars:
            raise ValueError("chunk_overlap_chars must be non-negative and smaller than chunk_chars")
        if not 0 <= self.keyword_threshold <= 1:
            raise ValueError("keyword_threshold must be between zero and one")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> DepartmentKnowledgeSettings:
        env = os.environ if environ is None else environ
        return cls(
            postgres_url=(env.get("AGENTSEEK_DEPARTMENT_KNOWLEDGE_POSTGRES_URL") or "").strip(),
            tenant_id=(env.get("AGENTSEEK_DEPARTMENT_KNOWLEDGE_TENANT_ID") or "strategic-development").strip(),
            collection_id=(
                env.get("AGENTSEEK_DEPARTMENT_KNOWLEDGE_COLLECTION_ID") or "strategic-development"
            ).strip(),
            owning_org=(env.get("AGENTSEEK_DEPARTMENT_KNOWLEDGE_OWNING_ORG") or "战略发展部").strip(),
            table_prefix=(env.get("AGENTSEEK_DEPARTMENT_KNOWLEDGE_TABLE_PREFIX") or "department_knowledge").strip(),
            dims=_env_int(env, "AGENTSEEK_DEPARTMENT_KNOWLEDGE_DIMS", DEFAULT_DIMS),
            onnx_model_path=(
                env.get("AGENTSEEK_DEPARTMENT_KNOWLEDGE_ONNX_MODEL_PATH")
                or env.get("AGENTSEEK_CTX_BGE_M3_ONNX_MODEL_PATH")
                or ""
            ).strip(),
            tokenizer_path=(
                env.get("AGENTSEEK_DEPARTMENT_KNOWLEDGE_TOKENIZER_PATH")
                or env.get("AGENTSEEK_CTX_BGE_M3_TOKENIZER_PATH")
                or ""
            ).strip(),
            max_length=_env_int(env, "AGENTSEEK_DEPARTMENT_KNOWLEDGE_MAX_LENGTH", 8192),
            chunk_chars=_env_int(env, "AGENTSEEK_DEPARTMENT_KNOWLEDGE_CHUNK_CHARS", 1200),
            chunk_overlap_chars=_env_int(env, "AGENTSEEK_DEPARTMENT_KNOWLEDGE_CHUNK_OVERLAP_CHARS", 150),
            keyword_threshold=_env_float(env, "AGENTSEEK_DEPARTMENT_KNOWLEDGE_KEYWORD_THRESHOLD", 0.08),
            create_hnsw_index=_truthy(env.get("AGENTSEEK_DEPARTMENT_KNOWLEDGE_CREATE_HNSW_INDEX", "true")),
        )

    @property
    def documents_table(self) -> str:
        return f"{self.table_prefix}_documents"

    @property
    def chunks_table(self) -> str:
        return f"{self.table_prefix}_chunks"


def _env_int(env: Mapping[str, str], key: str, default: int) -> int:
    return int(env.get(key, str(default)))


def _env_float(env: Mapping[str, str], key: str, default: float) -> float:
    return float(env.get(key, str(default)))


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}
