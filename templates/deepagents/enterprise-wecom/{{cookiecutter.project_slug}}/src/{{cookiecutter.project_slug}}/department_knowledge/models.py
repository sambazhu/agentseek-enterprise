from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class SearchMode(StrEnum):
    KEYWORD = "keyword"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"


@dataclass(frozen=True, slots=True)
class KnowledgeDocument:
    document_id: str
    title: str
    source_name: str
    source_sha256: str
    text: str
    confidentiality_level: str = "internal"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("document_id", "title", "source_name", "source_sha256", "text"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be non-blank text")
        if len(self.source_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.source_sha256
        ):
            raise ValueError("source_sha256 must be a lowercase SHA-256 digest")
        if self.confidentiality_level not in {"internal", "confidential"}:
            raise ValueError("department knowledge must be internal or confidential")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class KnowledgeChunk:
    chunk_id: str
    document_id: str
    ordinal: int
    heading: str
    content: str

    def __post_init__(self) -> None:
        if not self.chunk_id.strip() or not self.document_id.strip() or not self.content.strip():
            raise ValueError("knowledge chunk identifiers and content must not be blank")
        if self.ordinal < 0:
            raise ValueError("knowledge chunk ordinal must be non-negative")


@dataclass(frozen=True, slots=True)
class KnowledgeDocumentSummary:
    document_id: str
    title: str
    source_name: str
    source_sha256: str
    confidentiality_level: str
    chunk_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "title": self.title,
            "source_name": self.source_name,
            "source_sha256": self.source_sha256,
            "confidentiality_level": self.confidentiality_level,
            "chunk_count": self.chunk_count,
        }


@dataclass(frozen=True, slots=True)
class KnowledgeSearchHit:
    document_id: str
    chunk_id: str
    title: str
    heading: str
    excerpt: str
    score: float
    keyword_score: float | None = None
    semantic_score: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "chunk_id": self.chunk_id,
            "title": self.title,
            "heading": self.heading,
            "excerpt": self.excerpt,
            "score": round(self.score, 6),
            "keyword_score": _optional_score(self.keyword_score),
            "semantic_score": _optional_score(self.semantic_score),
        }


def _optional_score(value: float | None) -> float | None:
    return round(value, 6) if value is not None else None
