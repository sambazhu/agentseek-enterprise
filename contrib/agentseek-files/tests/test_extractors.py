from __future__ import annotations

import asyncio
import io
import json
import zipfile

import agentseek_files.extractors.mineru as mineru_module
import httpx
import pytest
from agentseek_files.context import build_current_files_context
from agentseek_files.extractors.local import LocalTextExtractor
from agentseek_files.extractors.mineru import MinerUExtractor, has_image_references, has_substantive_text
from agentseek_files.inbound import InboundFileService
from agentseek_files.models import ExtractResult, FileScope
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


def test_mineru_v4_upload_ocr_poll_and_markdown_zip(tmp_path, monkeypatch):
    uploaded: list[bytes] = []
    submitted: list[dict] = []
    debug_messages: list[str] = []

    class FakeLogger:
        def debug(self, message: str, *args: object) -> None:
            debug_messages.append(message.format(*args))

    monkeypatch.setattr(mineru_module, "logger", FakeLogger())
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
    assert pending.metadata["ocr_mode"] == "forced"
    assert submitted == [
        {
            "files": [{"name": "scan.pdf", "data_id": record.file_id, "is_ocr": True}],
            "model_version": "pipeline",
            "language": "ch",
            "enable_table": True,
            "enable_formula": True,
        }
    ]
    assert uploaded == [pdf_bytes]
    assert done.status == "done"
    assert done.markdown == "扫描件 OCR 结果"
    assert done.chars == 10
    debug_output = "\n".join(debug_messages)
    assert '"is_ocr": true' in debug_output
    assert '"model_version": "pipeline"' in debug_output
    assert "mineru-test-token" not in debug_output
    assert "scan.pdf" not in debug_output


def test_mineru_auto_detect_retries_image_only_pdf_with_pipeline_ocr(tmp_path, monkeypatch):
    submitted: list[dict] = []
    uploaded: list[bytes] = []
    first_zip = _markdown_zip("![](images/page-1.jpg)\n<!-- image -->")
    ocr_text = "这是扫描 PDF 的 OCR 中文正文。" * 12
    second_zip = _markdown_zip(ocr_text)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            submitted.append(json.loads(request.content))
            attempt = len(submitted)
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "batch_id": f"batch-{attempt}",
                        "file_urls": [f"https://upload.example.com/file-{attempt}"],
                    },
                },
            )
        if request.url.host == "upload.example.com":
            uploaded.append(request.content)
            return httpx.Response(200)
        if request.url.path == "/api/v4/extract-results/batch/batch-1":
            return _done_batch_response("batch-1", "https://result.example.com/first.zip")
        if request.url.path == "/api/v4/extract-results/batch/batch-2":
            return _done_batch_response("batch-2", "https://result.example.com/second.zip")
        if request.url.path == "/first.zip":
            return httpx.Response(200, content=first_zip)
        if request.url.path == "/second.zip":
            return httpx.Response(200, content=second_zip)
        raise AssertionError

    settings = FilesSettings(
        root_dir=tmp_path,
        allowed_extensions=(".pdf",),
        extractor="mineru",
        mineru_base_url="https://mineru.example.com",
        mineru_token="mineru-test-token",
        mineru_model_version="vlm",
        mineru_ocr_model_version="pipeline",
        mineru_is_ocr=False,
    )
    service = InboundFileService(settings)
    pdf_bytes = b"%PDF-1.4\nscanned"

    async def run_flow():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            monkeypatch.setattr(service, "_mineru_extractor", MinerUExtractor(settings, client=client))
            pending = await service.handle_bytes(
                scope=FileScope("hmac-t", "hmac-e", "hmac-s"),
                filename="scan.pdf",
                data=pdf_bytes,
                mime_type="application/pdf",
            )
            first_task_id = pending.record.extract_task_id
            retried = await service.poll_pending(pending.record)
            done = await service.poll_pending(retried.record)
            return first_task_id, retried, done

    first_task_id, retried, done = asyncio.run(run_flow())

    assert first_task_id == "batch-1"
    assert retried.record.extract_task_id == "batch-2"
    assert retried.record.metadata["extract"]["ocr_attempt"] == 2
    assert submitted[0]["files"][0]["is_ocr"] is False
    assert submitted[0]["model_version"] == "vlm"
    assert submitted[1]["files"][0]["is_ocr"] is True
    assert submitted[1]["model_version"] == "pipeline"
    assert uploaded == [pdf_bytes, pdf_bytes]
    assert done.record.extract_status == "done"
    assert done.extract_text == ocr_text


def test_mineru_mixed_pdf_returns_first_pass_then_replaces_it_with_background_ocr(
    tmp_path,
    monkeypatch,
):
    submitted: list[dict] = []
    first_text = ("这是数字文字页的快速提取结果。" * 12) + "\n![](images/scanned-page.jpg)"
    complete_text = ("这是数字文字页的完整结果。" * 12) + "\n这是扫描页 OCR 补充的中文内容。"
    first_zip = _markdown_zip(first_text)
    complete_zip = _markdown_zip(complete_text)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            submitted.append(json.loads(request.content))
            attempt = len(submitted)
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "batch_id": f"batch-{attempt}",
                        "file_urls": [f"https://upload.example.com/file-{attempt}"],
                    },
                },
            )
        if request.url.host == "upload.example.com":
            return httpx.Response(200)
        if request.url.path == "/api/v4/extract-results/batch/batch-1":
            return _done_batch_response("batch-1", "https://result.example.com/first.zip")
        if request.url.path == "/api/v4/extract-results/batch/batch-2":
            return _done_batch_response("batch-2", "https://result.example.com/complete.zip")
        if request.url.path == "/first.zip":
            return httpx.Response(200, content=first_zip)
        if request.url.path == "/complete.zip":
            return httpx.Response(200, content=complete_zip)
        raise AssertionError

    settings = FilesSettings(
        root_dir=tmp_path,
        allowed_extensions=(".pdf",),
        extractor="mineru",
        mixed_pdf_bg_ocr=True,
        mineru_base_url="https://mineru.example.com",
        mineru_token="mineru-test-token",
        mineru_model_version="vlm",
        mineru_ocr_model_version="pipeline",
        mineru_is_ocr=False,
        mineru_poll_interval_s=0,
    )
    service = InboundFileService(settings)

    async def run_flow():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            monkeypatch.setattr(service, "_mineru_extractor", MinerUExtractor(settings, client=client))
            pending = await service.handle_bytes(
                scope=FileScope("hmac-t", "hmac-e", "hmac-s"),
                filename="mixed.pdf",
                data=b"%PDF-1.4\nmixed",
                mime_type="application/pdf",
            )
            first_pass = await service.poll_pending(pending.record)
            assert first_pass.pending is False
            assert first_pass.extract_text == first_text
            assert first_pass.record.extract_status == "done"
            assert first_pass.record.mixed_pdf_bg_ocr is True
            assert first_pass.record.bg_ocr_status == "pending"
            await asyncio.gather(*tuple(service._background_tasks))
            return first_pass.record

    first_record = asyncio.run(run_flow())
    final_record = service.store.load_record(first_record.relative_dir)

    assert submitted[0]["files"][0]["is_ocr"] is False
    assert submitted[0]["model_version"] == "vlm"
    assert submitted[1]["files"][0]["is_ocr"] is True
    assert submitted[1]["model_version"] == "pipeline"
    assert final_record.extract_status == "done"
    assert final_record.extract_task_id == "batch-2"
    assert final_record.mixed_pdf_bg_ocr is True
    assert final_record.bg_ocr_status == "done"
    assert final_record.bg_ocr_task_id == "batch-2"
    stored_metadata = json.loads((tmp_path / final_record.relative_dir / "metadata.json").read_text())
    assert stored_metadata["mixed_pdf_bg_ocr"] is True
    assert stored_metadata["bg_ocr_status"] == "done"
    assert stored_metadata["bg_ocr_task_id"] == "batch-2"
    assert service.store.load_extract_text(final_record) == complete_text


def test_mineru_mixed_pdf_background_failure_preserves_first_pass(tmp_path, monkeypatch):
    settings = FilesSettings(
        root_dir=tmp_path,
        allowed_extensions=(".pdf",),
        extractor="mineru",
        mixed_pdf_bg_ocr=True,
    )
    service = InboundFileService(settings)
    record = service.store.store_bytes(
        scope=FileScope("hmac-t", "hmac-e", "hmac-s"),
        filename="mixed.pdf",
        data=b"%PDF-1.4\nmixed",
        mime_type="application/pdf",
    )
    first_text = ("第一遍可用的数字文字。" * 12) + "\n![](images/scanned-page.jpg)"
    record = service.store.save_extract(
        record,
        ExtractResult(
            file_id=record.file_id,
            provider="mineru",
            status="done",
            markdown=first_text,
            text=first_text,
            chars=len(first_text),
            provider_task_id="batch-1",
            metadata={"mode": "extract_batch", "ocr_mode": "auto", "is_ocr": False},
        ),
    )

    async def fail_retry(*args, **kwargs):
        del args, kwargs
        raise RuntimeError

    monkeypatch.setattr(service._mineru_extractor, "retry_with_ocr", fail_retry)

    async def run_background():
        service._schedule_mixed_pdf_background_ocr(record)
        await asyncio.gather(*tuple(service._background_tasks))

    asyncio.run(run_background())
    final_record = service.store.load_record(record.relative_dir)

    assert final_record.extract_status == "done"
    assert final_record.extract_task_id == "batch-1"
    assert final_record.mixed_pdf_bg_ocr is True
    assert final_record.bg_ocr_status == "failed"
    assert service.store.load_extract_text(final_record) == first_text


def test_mineru_digital_pdf_skips_mixed_background_ocr(tmp_path, monkeypatch):
    settings = FilesSettings(
        root_dir=tmp_path,
        allowed_extensions=(".pdf",),
        extractor="mineru",
        mixed_pdf_bg_ocr=True,
    )
    service = InboundFileService(settings)
    record = service.store.store_bytes(
        scope=FileScope("hmac-t", "hmac-e", "hmac-s"),
        filename="digital.pdf",
        data=b"%PDF-1.4\ndigital",
        mime_type="application/pdf",
    )
    record = service.store.save_extract(
        record,
        ExtractResult(
            file_id=record.file_id,
            provider="mineru",
            status="pending",
            provider_task_id="batch-1",
            metadata={"mode": "extract_batch", "ocr_mode": "auto", "is_ocr": False},
        ),
    )
    digital_text = "纯数字 PDF 的有效正文。" * 12

    async def finish_first_pass(*args, **kwargs):
        del args, kwargs
        return ExtractResult(
            file_id=record.file_id,
            provider="mineru",
            status="done",
            markdown=digital_text,
            text=digital_text,
            chars=len(digital_text),
            provider_task_id="batch-1",
            metadata={
                "mode": "extract_batch",
                "ocr_mode": "auto",
                "is_ocr": False,
                "state": "done",
            },
        )

    monkeypatch.setattr(service._mineru_extractor, "poll_result", finish_first_pass)

    result = asyncio.run(service.poll_pending(record))

    assert result.pending is False
    assert result.extract_text == digital_text
    assert result.record.mixed_pdf_bg_ocr is False
    assert result.record.bg_ocr_status == "skipped"
    assert not service._background_tasks


def test_mineru_substantive_text_ignores_image_only_placeholders() -> None:
    assert has_substantive_text("![](images/one.jpg)\n<!-- image -->\n<img src='two.jpg'>") is False
    assert has_substantive_text("有效的数字 PDF 正文内容。" * 12) is True
    assert has_image_references("正文\n![](images/one.jpg)") is True
    assert has_image_references("只有正文") is False


@pytest.mark.parametrize("extension", [".pdf", ".docx", ".pptx", ".xlsx"])
def test_mineru_mixed_background_ocr_supports_pdf_and_office_formats(tmp_path, extension: str) -> None:
    settings = FilesSettings(root_dir=tmp_path, allowed_extensions=(extension,), extractor="mineru")
    store = LocalFileStore(settings)
    record = store.store_bytes(
        scope=FileScope("hmac-t", "hmac-e", "hmac-s"),
        filename=f"mixed{extension}",
        data=b"mixed office fixture",
        mime_type="application/octet-stream",
    )
    record.metadata["extract"] = {
        "ocr_mode": "auto",
        "is_ocr": False,
    }
    content = ("这是第一遍提取出的实质文字。" * 12) + "\n![](images/table.png)"
    result = ExtractResult(
        file_id=record.file_id,
        provider="mineru",
        status="done",
        markdown=content,
        text=content,
        chars=len(content),
    )
    extractor = MinerUExtractor(settings)

    assert extractor.is_auto_non_ocr_pdf_result(record, result) is True
    assert extractor.should_run_mixed_pdf_background_ocr(record, result) is True


def _markdown_zip(markdown: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("result/full.md", markdown)
    return buffer.getvalue()


def _done_batch_response(batch_id: str, full_zip_url: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "code": 0,
            "data": {
                "batch_id": batch_id,
                "extract_result": [{"state": "done", "full_zip_url": full_zip_url}],
            },
        },
    )
