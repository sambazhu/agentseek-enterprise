from __future__ import annotations

import asyncio

import httpx
from agentseek_files.context import build_current_files_context
from agentseek_files.extractors.local import LocalTextExtractor
from agentseek_files.extractors.mineru import MinerUExtractor
from agentseek_files.models import FileScope
from agentseek_files.settings import FilesSettings
from agentseek_files.store import LocalFileStore


def test_local_text_extractor_truncates_and_builds_context(tmp_path):
    store = LocalFileStore(FilesSettings(root_dir=tmp_path, allowed_extensions=(".md",)))
    record = store.store_bytes(
        scope=FileScope("hmac-t", "hmac-e", "hmac-s"),
        filename="note.md",
        data="第一行\n第二行\n第三行".encode(),
        mime_type="text/markdown",
    )
    extractor = LocalTextExtractor(max_chars=5)

    result = asyncio.run(extractor.extract(record, store.original_path(record)))
    updated = store.save_extract(record, result)

    assert result.status == "done"
    assert result.chars == 5
    assert updated.extract_status == "done"
    context = build_current_files_context([updated], {updated.file_id: result.markdown})
    assert "[CurrentFiles]" in context
    assert "note.md" in context
    assert str(tmp_path) not in context


def test_mineru_async_client_uploads_bytes_without_sync_stream_error(tmp_path):
    uploaded: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"task_id": "task-1", "file_url": "https://upload.example.com/file"},
                },
            )
        uploaded.append(request.content)
        return httpx.Response(200)

    async def run_extract(record, source_path):
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            extractor = MinerUExtractor(
                FilesSettings(root_dir=tmp_path, extractor="mineru", mineru_base_url="https://mineru.example.com"),
                client=client,
            )
            return await extractor.extract(record, source_path)

    store = LocalFileStore(FilesSettings(root_dir=tmp_path, allowed_extensions=(".pdf",)))
    pdf_bytes = b"%PDF-1.7\nmock"
    record = store.store_bytes(
        scope=FileScope("hmac-t", "hmac-e", "hmac-s"),
        filename="report.pdf",
        data=pdf_bytes,
        mime_type="application/pdf",
    )

    result = asyncio.run(run_extract(record, store.original_path(record)))

    assert result.status == "pending"
    assert result.provider_task_id == "task-1"
    assert uploaded == [pdf_bytes]
