from __future__ import annotations

from agentseek_files.context import build_current_files_context
from agentseek_files.models import FileRecord


def test_current_files_marks_parsed_and_unparsed_images() -> None:
    record = _record()
    text = (
        "![](images/balance-sheet.jpg)\n"
        "填报单位：证券有限公司（母公司）\n"
        "日期：2025年6月30日\n"
        "## 资产负债表\n"
        "<table><tr><td>货币资金</td><td>期末余额</td><td>资产</td><td>负债</td></tr></table>\n"
        "![](images/seal.jpg)\n"
    )

    context = build_current_files_context([record], {record.file_id: text}, max_chars_per_file=12_000)

    assert "image_refs: 2" in context
    assert "image_ocr_parsed: 1" in context
    assert "image_ocr_unparsed: 1" in context
    assert "[ImageOCR status=parsed]" in context
    assert "[ImageOCR status=unparsed]" in context
    assert "资产负债表" in context
    assert "货币资金" in context
    assert "parsed_image_1:" in context
    assert "不得猜测图片内容" in context
    assert "可能是 logo" not in context


def test_current_files_marks_long_plain_ocr_text_as_parsed() -> None:
    record = _record()
    ocr_text = "图片中的有效中文 OCR 内容" * 12
    text = f"![](images/scanned-page.png)\n{ocr_text}"

    context = build_current_files_context([record], {record.file_id: text})

    assert "image_ocr_parsed: 1" in context
    assert "image_ocr_unparsed: 0" in context
    assert "[ImageOCR status=parsed]" in context


def test_current_files_does_not_mark_short_caption_as_usable_ocr() -> None:
    record = _record()
    text = "![](images/logo.png)\n公司标志"

    context = build_current_files_context([record], {record.file_id: text})

    assert "image_ocr_parsed: 0" in context
    assert "image_ocr_unparsed: 1" in context
    assert "[ImageOCR status=unparsed]" in context


def test_current_files_leaves_text_only_extract_without_image_analysis() -> None:
    record = _record()

    context = build_current_files_context([record], {record.file_id: "纯文字文档内容"})

    assert "纯文字文档内容" in context
    assert "image_refs:" not in context
    assert "ImageOCR" not in context


def _record() -> FileRecord:
    return FileRecord(
        file_id="file_context",
        direction="inbound",
        tenant_key="tenant",
        employee_key="employee",
        session_key="session",
        date="2026-07-10",
        filename="mixed.docx",
        sanitized_filename="mixed.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=1024,
        sha256="abc",
        relative_dir="tenant/employee/file_context",
        created_at="2026-07-10T00:00:00+00:00",
        extract_status="done",
    )
