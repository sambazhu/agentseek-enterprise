#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from {{ cookiecutter.project_slug }}.department_knowledge.ingestion import chunk_document, load_document
from {{ cookiecutter.project_slug }}.department_knowledge.models import SearchMode
from {{ cookiecutter.project_slug }}.department_knowledge.repository import DepartmentKnowledgeRepository
from {{ cookiecutter.project_slug }}.department_knowledge.settings import DepartmentKnowledgeSettings


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe the local department-knowledge MCP storage contract.")
    parser.add_argument("--query", required=True, help="A keyword or conceptual query expected in the test documents")
    parser.add_argument("--source", type=Path, help="Optional trusted document to import before searching")
    parser.add_argument("--mode", choices=tuple(SearchMode), default=SearchMode.HYBRID)
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    settings = DepartmentKnowledgeSettings.from_env()
    repository = DepartmentKnowledgeRepository(settings)
    repository.initialize()
    imported: dict[str, object] | None = None
    if args.source:
        document = load_document(
            args.source,
            namespace=f"{settings.tenant_id}/{settings.collection_id}",
        )
        chunks = chunk_document(
            document,
            max_chars=settings.chunk_chars,
            overlap_chars=settings.chunk_overlap_chars,
        )
        imported = repository.upsert_document(document, chunks).as_dict()

    hits = repository.search(args.query, mode=SearchMode(args.mode), limit=args.limit)
    selected = repository.read_chunks([hits[0].chunk_id]) if hits else ()
    payload = {
        "ok": bool(hits),
        "collection_id": settings.collection_id,
        "owning_org": settings.owning_org,
        "imported": imported,
        "document_count": len(repository.list_documents()),
        "hits": [
            {
                "document_id": hit.document_id,
                "chunk_id": hit.chunk_id,
                "score": hit.score,
                "keyword_score": hit.keyword_score,
                "semantic_score": hit.semantic_score,
                "excerpt_chars": len(hit.excerpt),
            }
            for hit in hits
        ],
        "selected_chunk_chars": len(str(selected[0]["content"])) if selected else 0,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if hits else 1


if __name__ == "__main__":
    raise SystemExit(main())
