from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from enterprise_wecom_digital_employee.department_knowledge.ingestion import (
    chunk_document,
    extract_docx_text,
    load_document,
)
from enterprise_wecom_digital_employee.department_knowledge.mcp_server import build_mcp
from enterprise_wecom_digital_employee.department_knowledge.models import (
    KnowledgeDocument,
    KnowledgeDocumentSummary,
    KnowledgeSearchHit,
    SearchMode,
)
from enterprise_wecom_digital_employee.department_knowledge.repository import (
    DepartmentKnowledgeRepository,
    _reciprocal_rank_fusion,
)
from enterprise_wecom_digital_employee.department_knowledge.settings import DepartmentKnowledgeSettings


def test_settings_reuse_contextseek_bge_paths_and_fix_scope() -> None:
    settings = DepartmentKnowledgeSettings.from_env(
        {
            "AGENTSEEK_DEPARTMENT_KNOWLEDGE_POSTGRES_URL": "postgresql://app:secret@localhost/agentseek",
            "AGENTSEEK_CTX_BGE_M3_ONNX_MODEL_PATH": "models/bge-m3/model.onnx",
            "AGENTSEEK_CTX_BGE_M3_TOKENIZER_PATH": "models/bge-m3/tokenizer.json",
        }
    )

    assert settings.tenant_id == "strategic-development"
    assert settings.collection_id == "strategic-development"
    assert settings.owning_org == "战略发展部"
    assert settings.onnx_model_path.endswith("model.onnx")
    assert settings.tokenizer_path.endswith("tokenizer.json")
    assert settings.documents_table == "department_knowledge_documents"


def test_docx_extraction_preserves_headings_paragraphs_and_tables() -> None:
    content = _docx(
        """
        <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>行业判断</w:t></w:r></w:p>
        <w:p><w:r><w:t>证券行业正在推进数字化转型。</w:t></w:r></w:p>
        <w:tbl><w:tr><w:tc><w:p><w:r><w:t>指标</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>结论</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
        """
    )

    text = extract_docx_text(content)

    assert "# 行业判断" in text
    assert "证券行业正在推进数字化转型。" in text
    assert "指标 | 结论" in text


def test_load_and_chunk_document_is_deterministic_bounded_and_heading_aware(tmp_path: Path) -> None:
    source = tmp_path / "战略规划.md"
    source.write_text("# 总体判断\n\n" + "长期能力建设。" * 80 + "\n\n## 风险\n\n关注执行风险。", encoding="utf-8")
    document = load_document(source, namespace="tenant/strategic-development")

    first = chunk_document(document, max_chars=180, overlap_chars=20)
    replay = chunk_document(document, max_chars=180, overlap_chars=20)

    assert first == replay
    assert len(first) > 2
    assert all(len(chunk.content) <= 180 for chunk in first)
    assert first[0].heading == "总体判断"
    assert first[-1].heading == "风险"
    assert document.document_id.startswith("dk_")


def test_load_document_rejects_unsupported_employee_style_upload(tmp_path: Path) -> None:
    source = tmp_path / "untrusted.exe"
    source.write_bytes(b"not knowledge")

    with pytest.raises(ValueError, match=r"supports \.docx"):
        load_document(source, namespace="tenant/strategic-development")


def test_hybrid_search_uses_reciprocal_rank_fusion_and_preserves_component_scores() -> None:
    keyword = (
        _hit("chunk-a", keyword=0.9),
        _hit("chunk-b", keyword=0.8),
    )
    semantic = (
        _hit("chunk-b", semantic=0.95),
        _hit("chunk-c", semantic=0.7),
    )

    fused = _reciprocal_rank_fusion(keyword, semantic, limit=3)

    assert [hit.chunk_id for hit in fused] == ["chunk-b", "chunk-a", "chunk-c"]
    assert fused[0].keyword_score == 0.8
    assert fused[0].semantic_score == 0.95


@pytest.mark.anyio
async def test_mcp_contract_is_read_only_and_does_not_expose_scope_arguments() -> None:
    repository = _FakeRepository()
    mcp = build_mcp(repository)

    tools = {tool.name: tool for tool in await mcp.list_tools()}

    assert set(tools) == {
        "knowledge_list_documents",
        "knowledge_search",
        "knowledge_read_chunks",
    }
    for tool in tools.values():
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert "tenant" not in str(tool.parameters).lower()
        assert "collection" not in str(tool.parameters).lower()
    search_properties = tools["knowledge_search"].parameters["properties"]
    assert set(search_properties) == {"query", "search_mode", "top_k"}

    result = await mcp.call_tool(
        "knowledge_search",
        {"query": "证券行业", "search_mode": "hybrid", "top_k": 6},
    )
    assert result.structured_content is not None
    assert result.structured_content["collection_id"] == "strategic-development"
    assert repository.searches == [("证券行业", SearchMode.HYBRID, 6)]


@pytest.mark.skipif(
    not os.environ.get("AGENTSEEK_DEPARTMENT_KNOWLEDGE_TEST_URL"),
    reason="set AGENTSEEK_DEPARTMENT_KNOWLEDGE_TEST_URL for the real PostgreSQL integration test",
)
def test_real_postgres_keyword_semantic_hybrid_and_collection_isolation() -> None:
    settings = DepartmentKnowledgeSettings(
        postgres_url=os.environ["AGENTSEEK_DEPARTMENT_KNOWLEDGE_TEST_URL"],
        tenant_id="pytest-department-knowledge",
        collection_id="collection-a",
        table_prefix="department_knowledge_test",
        dims=3,
        create_hnsw_index=False,
    )
    other_settings = DepartmentKnowledgeSettings(
        postgres_url=settings.postgres_url,
        tenant_id=settings.tenant_id,
        collection_id="collection-b",
        table_prefix=settings.table_prefix,
        dims=3,
        create_hnsw_index=False,
    )
    embedder = _FakeEmbedder()
    repository = DepartmentKnowledgeRepository(settings, embedder=embedder)
    other = DepartmentKnowledgeRepository(other_settings, embedder=embedder)
    repository.initialize()
    other.initialize()
    _delete_test_documents(settings)
    _delete_test_documents(other_settings)
    document = KnowledgeDocument(
        document_id="doc-securities",
        title="证券行业规划",
        source_name="证券行业规划.md",
        source_sha256="a" * 64,
        text="证券行业数字化转型与数据治理。",
    )
    chunks = chunk_document(document, max_chars=100, overlap_chars=10)
    repository.upsert_document(document, chunks)

    assert repository.search("数据治理", search_mode="keyword")[0].document_id == document.document_id
    assert repository.search("行业能力建设", search_mode="semantic")[0].document_id == document.document_id
    assert repository.search("证券行业", search_mode="hybrid")[0].document_id == document.document_id
    assert other.search("证券行业", search_mode="hybrid") == ()
    assert repository.read_chunks([chunks[0].chunk_id])[0]["content"] == document.text
    _delete_test_documents(settings)


class _FakeRepository:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(owning_org="战略发展部", collection_id="strategic-development")
        self.searches: list[tuple[str, SearchMode, int]] = []

    def list_documents(self, *, limit: int = 20) -> tuple[KnowledgeDocumentSummary, ...]:
        del limit
        return (
            KnowledgeDocumentSummary("doc-1", "规划", "规划.docx", "a" * 64, "internal", 2),
        )

    def search(
        self,
        query: str,
        *,
        search_mode: SearchMode,
        top_k: int,
    ) -> tuple[KnowledgeSearchHit, ...]:
        self.searches.append((query, SearchMode(search_mode), top_k))
        return (_hit("chunk-a", keyword=1.0),)

    def read_chunks(self, chunk_ids: Sequence[str]) -> tuple[dict[str, Any], ...]:
        return tuple({"chunk_id": value, "content": "内部材料"} for value in chunk_ids)


@dataclass
class _FakeEmbedder:
    dims: int = 3

    def embed(self, text: str) -> list[float]:
        if "数据" in text or "能力" in text:
            return [1.0, 0.0, 0.0]
        return [0.0, 1.0, 0.0]


def _hit(
    chunk_id: str,
    *,
    keyword: float | None = None,
    semantic: float | None = None,
) -> KnowledgeSearchHit:
    score = keyword if keyword is not None else semantic or 0.0
    return KnowledgeSearchHit(
        document_id="doc-1",
        chunk_id=chunk_id,
        title="战略规划",
        heading="行业判断",
        excerpt="内部知识摘要",
        score=score,
        keyword_score=keyword,
        semantic_score=semantic,
    )


def _docx(body_xml: str) -> bytes:
    document_xml = f"""
    <?xml version="1.0" encoding="UTF-8" standalone="yes"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body>{body_xml}<w:sectPr/></w:body>
    </w:document>
    """.strip()
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document_xml)
    return buffer.getvalue()


def _delete_test_documents(settings: DepartmentKnowledgeSettings) -> None:
    import psycopg
    from psycopg import sql

    url = settings.postgres_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(url) as connection, connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("DELETE FROM {} WHERE tenant_id = %s AND collection_id = %s").format(
                sql.Identifier(settings.documents_table)
            ),
            (settings.tenant_id, settings.collection_id),
        )
        connection.commit()
