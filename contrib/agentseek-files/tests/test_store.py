from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from agentseek_files.models import FileRecord, FileScope
from agentseek_files.settings import FilesSettings
from agentseek_files.store import FileStoreError, LocalFileStore, sanitize_filename


def test_store_bytes_uses_scoped_hmac_directories(tmp_path):
    store = LocalFileStore(FilesSettings(root_dir=tmp_path, allowed_extensions=(".txt",)))
    scope = FileScope(
        tenant_key="hmac-tenant",
        employee_key="hmac-employee",
        session_key="hmac-session",
        channel="wecom",
        chat_id="hmac-chat",
        message_id="hmac-msg",
    )

    record = store.store_bytes(
        scope=scope,
        filename="../secret report.txt",
        data=b"hello",
        mime_type="text/plain",
        now=datetime(2026, 7, 9, tzinfo=UTC),
    )

    assert record.relative_dir == "hmac-tenant/hmac-employee/2026-07-09/hmac-session/inbound/file_2cf24dba5fb0a30e"
    assert record.sanitized_filename == "secret_report.txt"
    assert (tmp_path / record.relative_dir / "original").read_bytes() == b"hello"
    metadata = json.loads((tmp_path / record.relative_dir / "metadata.json").read_text())
    assert metadata["chat_id"] == "hmac-chat"
    assert "secret report" in metadata["filename"]


def test_store_rejects_disallowed_extension(tmp_path):
    store = LocalFileStore(FilesSettings(root_dir=tmp_path, allowed_extensions=(".txt",)))
    scope = FileScope("hmac-t", "hmac-e", "hmac-s")

    with pytest.raises(FileStoreError, match="not allowed"):
        store.store_bytes(scope=scope, filename="report.exe", data=b"x")


@pytest.mark.parametrize(
    ("legacy_extension", "replacement"),
    [(".doc", ".docx"), (".xls", ".xlsx"), (".ppt", ".pptx")],
)
def test_store_recommends_modern_office_format(
    tmp_path,
    legacy_extension: str,
    replacement: str,
) -> None:
    store = LocalFileStore(FilesSettings(root_dir=tmp_path))

    with pytest.raises(FileStoreError) as error:
        store.store_bytes(
            scope=FileScope("hmac-t", "hmac-e", "hmac-s"),
            filename=f"legacy{legacy_extension}",
            data=b"legacy office",
        )

    assert error.value.user_notice == (
        f"暂不支持旧版 Office 格式 {legacy_extension}，请转换为 {replacement} 后重新上传。"
    )


@pytest.mark.parametrize(
    ("mime_type", "legacy_extension", "replacement"),
    [
        ("application/msword", ".doc", ".docx"),
        ("application/vnd.ms-excel", ".xls", ".xlsx"),
        ("application/vnd.ms-powerpoint", ".ppt", ".pptx"),
    ],
)
def test_store_recommends_modern_office_format_from_mime_without_filename(
    tmp_path,
    mime_type: str,
    legacy_extension: str,
    replacement: str,
) -> None:
    store = LocalFileStore(FilesSettings(root_dir=tmp_path))

    with pytest.raises(FileStoreError) as error:
        store.store_bytes(
            scope=FileScope("hmac-t", "hmac-e", "hmac-s"),
            filename="document",
            data=b"legacy office",
            mime_type=mime_type,
        )

    assert error.value.user_notice == (
        f"暂不支持旧版 Office 格式 {legacy_extension}，请转换为 {replacement} 后重新上传。"
    )


def test_store_adds_pdf_extension_from_mime_type_when_filename_has_none(tmp_path):
    store = LocalFileStore(FilesSettings(root_dir=tmp_path, allowed_extensions=(".pdf",)))
    scope = FileScope("hmac-tenant", "hmac-employee", "hmac-session")

    record = store.store_bytes(
        scope=scope,
        filename="document_20260710_120000",
        data=b"%PDF-1.7\nmock",
        mime_type="application/pdf; charset=binary",
    )

    assert record.filename == "document_20260710_120000.pdf"
    assert record.sanitized_filename == "document_20260710_120000.pdf"


def test_store_does_not_duplicate_existing_pdf_extension(tmp_path):
    store = LocalFileStore(FilesSettings(root_dir=tmp_path, allowed_extensions=(".pdf",)))
    record = store.store_bytes(
        scope=FileScope("hmac-tenant", "hmac-employee", "hmac-session"),
        filename="report.pdf",
        data=b"%PDF-1.7\nmock",
        mime_type="application/pdf",
    )

    assert record.filename == "report.pdf"
    assert record.sanitized_filename == "report.pdf"


def test_file_record_migrates_legacy_nested_background_ocr_metadata() -> None:
    payload = {
        "file_id": "file_1",
        "direction": "inbound",
        "tenant_key": "tenant",
        "employee_key": "employee",
        "session_key": "session",
        "date": "2026-07-10",
        "filename": "mixed.pdf",
        "sanitized_filename": "mixed.pdf",
        "mime_type": "application/pdf",
        "size_bytes": 1,
        "sha256": "abc",
        "relative_dir": "tenant/employee/file_1",
        "created_at": "2026-07-10T00:00:00+00:00",
        "metadata": {
            "mixed_pdf_bg_ocr": True,
            "bg_ocr_status": "done",
            "bg_ocr_task_id": "batch-2",
            "extract": {"mode": "extract_batch"},
        },
    }

    record = FileRecord.from_dict(payload)

    assert record.mixed_pdf_bg_ocr is True
    assert record.bg_ocr_status == "done"
    assert record.bg_ocr_task_id == "batch-2"
    assert record.metadata == {"extract": {"mode": "extract_batch"}}


def test_store_rejects_oversize_file(tmp_path):
    store = LocalFileStore(FilesSettings(root_dir=tmp_path, max_bytes=3, allowed_extensions=(".txt",)))
    scope = FileScope("hmac-t", "hmac-e", "hmac-s")

    with pytest.raises(FileStoreError, match="exceeds"):
        store.store_bytes(scope=scope, filename="report.txt", data=b"toolong")


def test_sanitize_filename_strips_path_and_unsafe_chars():
    assert sanitize_filename("../../企微 报告?.pdf") == "file.pdf"
    assert sanitize_filename("normal-file_1.txt") == "normal-file_1.txt"
