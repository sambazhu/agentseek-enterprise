from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from agentseek_files.content_analysis import analyze_content, format_large_file_summary
from agentseek_files.models import FileRecord

_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)(?:\s+[^)]*)?\)")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_TABLE_RE = re.compile(r"<table\b", re.IGNORECASE)
_MIN_OCR_TEXT_CHARS = 100
_PREVIEW_CHARS = 180
_LARGE_FILE_THRESHOLD = 50_000


@dataclass(frozen=True)
class _ImageOcrAnalysis:
    annotated_text: str
    total: int = 0
    parsed: int = 0
    previews: tuple[str, ...] = ()

    @property
    def unparsed(self) -> int:
        return self.total - self.parsed


def build_current_files_context(
    records: list[FileRecord],
    extracts: Mapping[str, str] | None = None,
    *,
    max_chars_per_file: int = 4_000,
) -> str:
    if not records:
        return ""
    extract_map = extracts or {}
    lines = ["[CurrentFiles]"]
    for record in records:
        lines.append(f"- file_id: {record.file_id}")
        lines.append(f"  filename: {record.sanitized_filename}")
        lines.append(f"  type: {record.mime_type}")
        lines.append(f"  size_bytes: {record.size_bytes}")
        lines.append(f"  extract_status: {record.extract_status}")
        text = extract_map.get(record.file_id, "")
        if text:
            if len(text) > _LARGE_FILE_THRESHOLD:
                content_analysis = analyze_content(text)
                lines.append(f"  extract_total_chars: {content_analysis.total_chars}")
                lines.append(f"  extract_total_lines: {content_analysis.total_lines}")
                lines.append("  extract_truncated: true")
                lines.append("  large_file_summary: |")
                for summary_line in format_large_file_summary(content_analysis):
                    lines.append(f"    {summary_line}")
                lines.append(
                    "  analysis_tool_hint: 全文未放入上下文；统计、分组、范围或全文检索问题，"
                    f'必须调用 analyze_file(file_id="{record.file_id}", question=<用户问题>)，不得按 excerpt 估算。'
                )
            image_analysis = _analyze_image_ocr(text)
            if image_analysis.total:
                lines.append(f"  image_refs: {image_analysis.total}")
                lines.append(f"  image_ocr_parsed: {image_analysis.parsed}")
                lines.append(f"  image_ocr_unparsed: {image_analysis.unparsed}")
                lines.append(
                    "  image_ocr_guidance: status=parsed 后面的文本/表格是图片 OCR 结果，"
                    "可直接用于回答；仅 status=unparsed 表示无可用图片内容，且不得猜测图片内容。"
                )
                if image_analysis.previews:
                    lines.append("  image_ocr_previews:")
                    for index, preview in enumerate(image_analysis.previews, start=1):
                        lines.append(f"    - parsed_image_{index}: {preview}")
            excerpt = image_analysis.annotated_text[:max_chars_per_file]
            lines.append(f"  excerpt_chars: {len(excerpt)}")
            lines.append("  excerpt: |")
            for line in excerpt.splitlines() or [excerpt]:
                lines.append(f"    {line}")
    lines.append("[/CurrentFiles]")
    return "\n".join(lines)


def _analyze_image_ocr(text: str) -> _ImageOcrAnalysis:
    matches = list(_MARKDOWN_IMAGE_RE.finditer(text))
    if not matches:
        return _ImageOcrAnalysis(annotated_text=text)

    annotated_parts: list[str] = []
    previews: list[str] = []
    parsed_count = 0
    cursor = 0
    for index, match in enumerate(matches):
        segment_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        following_content = text[match.end() : segment_end]
        parsed = _has_usable_ocr_content(following_content)
        annotated_parts.append(text[cursor : match.start()])
        if parsed:
            parsed_count += 1
            annotated_parts.append(
                "<!-- 图片已解析为文本/表格，以下为 OCR 提取结果 -->\n"
                "[ImageOCR status=parsed] 以下区段是该图片已解析的 OCR 文本/表格，"
                "请直接依据这些内容回答。[/ImageOCR]\n"
            )
            preview = _ocr_preview(following_content)
            if preview:
                previews.append(preview)
        else:
            annotated_parts.append(
                "<!-- 图片未获得可用 OCR 内容 -->\n"
                "[ImageOCR status=unparsed] 该图片没有可用 OCR 内容；不要猜测其类型或内容。[/ImageOCR]\n"
            )
        annotated_parts.append(match.group(0))
        annotated_parts.append(following_content)
        cursor = segment_end

    return _ImageOcrAnalysis(
        annotated_text="".join(annotated_parts),
        total=len(matches),
        parsed=parsed_count,
        previews=tuple(previews),
    )


def _has_usable_ocr_content(content: str) -> bool:
    if _TABLE_RE.search(content):
        return True
    plain_text = _plain_ocr_text(content)
    meaningful_chars = sum(character.isalnum() for character in plain_text)
    return meaningful_chars > _MIN_OCR_TEXT_CHARS


def _ocr_preview(content: str) -> str:
    return _plain_ocr_text(content)[:_PREVIEW_CHARS]


def _plain_ocr_text(content: str) -> str:
    without_tags = _HTML_TAG_RE.sub(" ", content)
    without_markdown = re.sub(r"[#*_`|]+", " ", without_tags)
    return " ".join(without_markdown.split())
