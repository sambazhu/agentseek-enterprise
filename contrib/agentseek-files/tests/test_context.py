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


def test_current_files_summarizes_complete_large_spreadsheet_without_expanding_excerpt() -> None:
    record = _record()
    record.filename = "退餐统计.xlsx"
    record.sanitized_filename = "report.xlsx"
    record.mime_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    rows = "".join(
        f"<tr><td>{index}</td><td>{'一一班' if index % 2 else '一二班'}</td>"
        f"<td>学生{index}</td><td>{index * 10}</td></tr>"
        for index in range(1, 1_501)
    )
    text = f"<table><tr><th>序号</th><th>班级</th><th>姓名</th><th>金额</th></tr>{rows}</table>"
    assert len(text) > 50_000

    context = build_current_files_context([record], {record.file_id: text}, max_chars_per_file=12_000)

    assert f"extract_total_chars: {len(text)}" in context
    assert "extract_truncated: true" in context
    assert "表格数据行数（不含表头）: 1500" in context
    assert "字段: 序号 | 班级 | 姓名 | 金额" in context
    assert "最后一条记录: 1500 | 一二班 | 学生1500 | 15000" in context
    assert "金额=10..15000" in context
    assert 'analyze_file(file_id="file_context"' in context
    assert "学生1500" not in context.split("  excerpt: |", maxsplit=1)[1]


def test_current_files_does_not_add_large_file_guidance_to_small_extract() -> None:
    record = _record()

    context = build_current_files_context([record], {record.file_id: "短文件"})

    assert "extract_truncated" not in context
    assert "analysis_tool_hint" not in context


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
