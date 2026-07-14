#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from enterprise_wecom_digital_employee.department_knowledge.ingestion import chunk_document, load_document
from enterprise_wecom_digital_employee.department_knowledge.repository import DepartmentKnowledgeRepository
from enterprise_wecom_digital_employee.department_knowledge.settings import DepartmentKnowledgeSettings

_SUPPORTED_SUFFIXES = {".docx", ".md", ".txt"}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Admin-only import of trusted department documents into the local MCP simulator."
    )
    parser.add_argument("paths", nargs="+", type=Path, help="Trusted .docx, .md, .txt files or directories")
    parser.add_argument(
        "--confidentiality",
        choices=("internal", "confidential"),
        default="internal",
    )
    args = parser.parse_args()

    settings = DepartmentKnowledgeSettings.from_env()
    repository = DepartmentKnowledgeRepository(settings)
    sources = _source_files(args.paths)
    if not sources:
        parser.error("no supported department knowledge documents were found")

    for source in sources:
        document = load_document(
            source,
            namespace=f"{settings.tenant_id}/{settings.collection_id}",
            confidentiality_level=args.confidentiality,
        )
        chunks = chunk_document(
            document,
            max_chars=settings.chunk_chars,
            overlap_chars=settings.chunk_overlap_chars,
        )
        summary = repository.upsert_document(document, chunks)
        print(
            f"imported document_id={summary.document_id} chunks={summary.chunk_count} "
            f"source={summary.source_name}"
        )
    return 0


def _source_files(paths: list[Path]) -> tuple[Path, ...]:
    selected: set[Path] = set()
    for value in paths:
        candidate = value.expanduser()
        if candidate.is_symlink():
            raise ValueError("knowledge import paths must not be symlinks")  # noqa: TRY003
        path = candidate.resolve(strict=True)
        if path.is_dir():
            selected.update(
                candidate
                for candidate in path.iterdir()
                if candidate.is_file()
                and not candidate.is_symlink()
                and candidate.suffix.lower() in _SUPPORTED_SUFFIXES
            )
        elif path.is_file() and path.suffix.lower() in _SUPPORTED_SUFFIXES:
            selected.add(path)
    return tuple(sorted(selected))


if __name__ == "__main__":
    raise SystemExit(main())
