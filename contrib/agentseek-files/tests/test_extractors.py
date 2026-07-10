from __future__ import annotations

import asyncio
import io
import json
import zipfile

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


def test_mineru_v4_upload_ocr_poll_and_markdown_zip(tmp_path):
    uploaded: list[bytes] = []
    submitted: list[dict] = []
    result_zip = io.BytesIO()
    with zipfile.ZipFile(result_zip, "w") as archive:
        archive.writestr("result/full.md", "扫描件 OCR 结果")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert request.url.path == "/api/v4/file-urls/batch"
            assert request.headers["authorization"] == "Bearer mineru-test-token"
            submitted.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "trace_id": "trace-submit",
                    "data": {"batch_id": "batch-1", "file_urls": ["https://upload.example.com/file"]},
                },
            )
        if request.url.host == "upload.example.com":
            assert "authorization" not in request.headers
            uploaded.append(request.content)
            return httpx.Response(200)
        if request.url.path == "/api/v4/extract-results/batch/batch-1":
            assert request.headers["authorization"] == "Bearer mineru-test-token"
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "trace_id": "trace-poll",
                    "data": {
                        "batch_id": "batch-1",
                        "extract_result": [
                            {
                                "data_id": "file_2f21b926ec3625f7",
                                "file_name": "scan.pdf",
                                "state": "done",
                                "full_zip_url": "https://result.example.com/result.zip",
                            }
                        ],
                    },
                },
            )
        if request.url.host == "result.example.com":
            return httpx.Response(200, content=result_zip.getvalue())
        raise AssertionError

    async def run_extract(record, source_path, store):
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            extractor = MinerUExtractor(
                FilesSettings(
                    root_dir=tmp_path,
                    extractor="mineru",
                    mineru_base_url="https://mineru.example.com",
                    mineru_token="mineru-test-token",
                    mineru_model_version="vlm",
                    mineru_language="ch",
                    mineru_enable_table=True,
                    mineru_enable_formula=True,
                    mineru_is_ocr=True,
                ),
                client=client,
            )
            pending = await extractor.extract(record, source_path)
            record = store.save_extract(record, pending)
            done = await extractor.poll_result(record, pending.provider_task_id or "")
            return pending, done

    store = LocalFileStore(FilesSettings(root_dir=tmp_path, allowed_extensions=(".pdf",)))
    pdf_bytes = b"%PDF-1.4\nscanned"
    record = store.store_bytes(
        scope=FileScope("hmac-t", "hmac-e", "hmac-s"),
        filename="scan.pdf",
        data=pdf_bytes,
        mime_type="application/pdf",
    )

    pending, done = asyncio.run(run_extract(record, store.original_path(record), store))

    assert pending.provider_task_id == "batch-1"
    assert pending.metadata["mode"] == "extract_batch"
    assert submitted == [
        {
            "files": [{"name": "scan.pdf", "data_id": record.file_id, "is_ocr": True}],
            "model_version": "vlm",
            "language": "ch",
            "enable_table": True,
            "enable_formula": True,
        }
    ]
    assert uploaded == [pdf_bytes]
    assert done.status == "done"
    assert done.markdown == "扫描件 OCR 结果"
    assert done.chars == 10
