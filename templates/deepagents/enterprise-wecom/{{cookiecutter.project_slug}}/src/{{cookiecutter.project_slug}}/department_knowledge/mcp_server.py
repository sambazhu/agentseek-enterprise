from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from fastmcp import FastMCP

from {{ cookiecutter.project_slug }}.department_knowledge.models import SearchMode
from {{ cookiecutter.project_slug }}.department_knowledge.repository import DepartmentKnowledgeRepository
from {{ cookiecutter.project_slug }}.department_knowledge.settings import DepartmentKnowledgeSettings

_READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


class KnowledgeRepository(Protocol):
    settings: Any

    def list_documents(self, *, limit: int = 20) -> Sequence[Any]: ...

    def search(self, query: str, *, search_mode: SearchMode, top_k: int) -> Sequence[Any]: ...

    def read_chunks(self, chunk_ids: Sequence[str]) -> Sequence[dict[str, Any]]: ...


def build_mcp(repository: KnowledgeRepository | None = None) -> FastMCP:
    repo = repository or DepartmentKnowledgeRepository(DepartmentKnowledgeSettings.from_env())
    mcp = FastMCP(
        "department-knowledge",
        instructions=(
            "Read-only Strategic Development Department knowledge. List or search internal documents first, "
            "then read only the selected chunks. Never treat employee uploads as shared department knowledge."
        ),
    )

    @mcp.tool(
        name="knowledge_list_documents",
        description="List documents currently published in this fixed department knowledge collection.",
        annotations=_READ_ONLY,
    )
    def knowledge_list_documents(limit: int = 20) -> dict[str, Any]:
        documents = repo.list_documents(limit=limit)
        return {
            "owning_org": repo.settings.owning_org,
            "collection_id": repo.settings.collection_id,
            "documents": [document.as_dict() for document in documents],
        }

    @mcp.tool(
        name="knowledge_search",
        description=(
            "Search the fixed department knowledge collection. Use hybrid by default; keyword is best for "
            "exact terms and semantic is best for conceptual questions. Set search_mode and top_k when needed. "
            "Results contain excerpts and chunk IDs."
        ),
        annotations=_READ_ONLY,
    )
    def knowledge_search(
        query: str,
        search_mode: SearchMode = SearchMode.HYBRID,
        top_k: int = 8,
    ) -> dict[str, Any]:
        hits = repo.search(query, search_mode=search_mode, top_k=top_k)
        return {
            "owning_org": repo.settings.owning_org,
            "collection_id": repo.settings.collection_id,
            "search_mode": SearchMode(search_mode).value,
            "hits": [hit.as_dict() for hit in hits],
        }

    @mcp.tool(
        name="knowledge_read_chunks",
        description=(
            "Read full text for selected chunk IDs returned by knowledge_search. Read only the chunks needed "
            "for the current report section."
        ),
        annotations=_READ_ONLY,
    )
    def knowledge_read_chunks(chunk_ids: Sequence[str]) -> dict[str, Any]:
        return {
            "owning_org": repo.settings.owning_org,
            "collection_id": repo.settings.collection_id,
            "chunks": list(repo.read_chunks(chunk_ids)),
        }

    return mcp


def main() -> None:
    build_mcp().run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
