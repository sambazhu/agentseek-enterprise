from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import ToolRuntime

from agentseek_files.content_analysis import (
    ContentAnalysis,
    analyze_content,
    format_large_file_summary,
    group_counts,
    group_counts_by_sheet,
    matching_rows,
    sheet_summaries,
    unique_people,
)
from agentseek_files.models import FileRecord
from agentseek_files.store import LocalFileStore

_MAX_TOOL_RESULT_CHARS = 12_000


def file_analysis_tools(store: LocalFileStore | None = None) -> list[BaseTool]:
    """Return read-only tools for analyzing complete extracted file text."""
    file_store = store or LocalFileStore()

    @tool("analyze_file")
    def analyze_file(file_id: str, question: str, runtime: ToolRuntime) -> str:
        """Analyze all parsed text for one current file, without reading its original binary.

        Use this when CurrentFiles says extract_truncated=true or the employee asks
        for totals, grouping, ranges, searches, or other whole-file facts. Pass the
        exact file_id shown in CurrentFiles and the employee's statistical question.
        The tool is read-only and can access only files in the current turn's state.
        """
        return _analyze_current_file(file_store, runtime.state, file_id=file_id, question=question)

    return [analyze_file]


def _analyze_current_file(
    store: LocalFileStore,
    state: object,
    *,
    file_id: str,
    question: str,
) -> str:
    loaded = _load_current_extract(store, state, file_id)
    if isinstance(loaded, str):
        return loaded
    record, text = loaded

    analysis = analyze_content(
        text,
        infer_sheet_names=record.sanitized_filename.lower().endswith(".xlsx"),
    )
    lines = [f"完整文件分析（file_id={file_id}）:", *format_large_file_summary(analysis)]
    people = unique_people(analysis)
    if people is not None:
        lines.append(f"唯一人员数（按姓名去重）: {people}")
    lines.extend(_format_sheet_summaries(analysis))

    grouped = group_counts(analysis, question)
    if grouped is not None:
        column, counts = grouped
        lines.extend(_format_group_counts(analysis, question, column, counts))
    else:
        match_count, samples = matching_rows(analysis, question)
        if match_count:
            lines.append(f"与问题关键词匹配的数据行: {match_count}")
            lines.extend(f"- {sample}" for sample in samples)

    lines.append("以上统计基于完整 extracted.md/extracted.txt，不是 CurrentFiles 截断片段。")
    return "\n".join(lines)[:_MAX_TOOL_RESULT_CHARS]


def _format_sheet_summaries(analysis: ContentAnalysis) -> list[str]:
    sheets = sheet_summaries(analysis)
    if not sheets:
        return []
    lines = ["按页签统计:"]
    for sheet in sheets[:50]:
        details = [f"数据行数={sheet.data_rows}", f"表格数={sheet.table_count}"]
        if sheet.unique_people is not None:
            details.append(f"唯一人员数={sheet.unique_people}")
        if sheet.headers:
            details.append(f"字段={' | '.join(sheet.headers)}")
        lines.append(f"- {sheet.sheet_name}: {', '.join(details)}")
    return lines


def _format_group_counts(
    analysis: ContentAnalysis,
    question: str,
    column: str,
    counts: list[tuple[str, int]],
) -> list[str]:
    lines = [f"按“{column}”统计:", *(f"- {name}: {count}" for name, count in counts[:200])]
    if len(counts) > 200:
        lines.append(f"- 其余 {len(counts) - 200} 组已省略")
    sheet_groups = group_counts_by_sheet(analysis, question)
    if sheet_groups:
        lines.append(f"各页签按“{column}”统计:")
        for sheet_group in sheet_groups[:50]:
            rendered = "; ".join(f"{name}={count}" for name, count in sheet_group.counts[:100])
            lines.append(f"- {sheet_group.sheet_name}: {rendered}")
    return lines


def _load_current_extract(
    store: LocalFileStore,
    state: object,
    file_id: str,
) -> tuple[FileRecord, str] | str:
    state_mapping = state if isinstance(state, Mapping) else {}
    current_files = state_mapping.get("current_files")
    if not isinstance(current_files, list):
        return "当前会话没有可分析的文件。"

    state_record = _find_current_record(current_files, file_id)
    if state_record is None:
        return f"文件 {file_id} 不在当前会话的 CurrentFiles 中，拒绝读取。"

    try:
        disk_record = store.load_record(state_record.relative_dir)
    except (OSError, ValueError, TypeError):
        return f"文件 {file_id} 的解析记录不可用。"
    if not _same_scope(state_record, disk_record):
        return f"文件 {file_id} 的作用域校验失败，拒绝读取。"

    try:
        text = store.load_extract_text(disk_record)
    except (OSError, UnicodeError, ValueError):
        return f"文件 {file_id} 的解析文本不可用。"
    if not text:
        return f"文件 {file_id} 尚无可分析的解析文本（status={disk_record.extract_status}）。"
    return disk_record, text


def _find_current_record(items: Sequence[object], file_id: str) -> FileRecord | None:
    for item in items:
        if isinstance(item, FileRecord) and item.file_id == file_id:
            return item
        if isinstance(item, dict) and item.get("file_id") == file_id:
            try:
                payload: dict[str, Any] = {str(key): value for key, value in item.items()}
                return FileRecord.from_dict(payload)
            except (TypeError, ValueError):
                return None
    return None


def _same_scope(expected: FileRecord, actual: FileRecord) -> bool:
    return (
        expected.file_id == actual.file_id
        and expected.relative_dir == actual.relative_dir
        and expected.tenant_key == actual.tenant_key
        and expected.employee_key == actual.employee_key
        and expected.session_key == actual.session_key
    )
