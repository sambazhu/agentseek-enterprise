from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence

from agentseek_enterprise.observability import emit_enterprise_event
from langchain_core.messages import BaseMessage, HumanMessage

_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
_GENERIC_CONFIRM_RE = re.compile(
    r"^(?:确认|同意|批准|认可|可以|好的|好|ok|okay)[。！!\s]*$",
    re.IGNORECASE,
)
_MARKDOWN_HEADING_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s+\S")
_MARKDOWN_TABLE_RE = re.compile(r"(?m)^\s*\|?.*\|.*\n\s*\|?\s*:?-{3,}")
_REPORT_BODY_PHRASES = (
    "完整报告",
    "报告正文",
    "报告已编写完成",
    "以下为报告",
    "以下为完整报告",
)
_REPORT_SECTION_LABELS = (
    "执行摘要",
    "行业发展概况",
    "经营差异",
    "业务线对标",
    "行动建议",
    "风险提示",
)
_MAX_M2_OUTPUT_CHARS = 1200

M2_OUTPUT_BLOCKED_MESSAGE = (
    "当前任务仍处于 M2 需求确认、内部研究和来源登记阶段，尚未启用报告正文生成。"
    "这次模型输出已被运行时守卫拦截，不作为报告或事实交付。"
    "请明确回复“确认 ReportBrief vN”，或从 get_report_research_gaps 返回的精确版本选项中选择一项。"
    "如果内部研究已无缺口，当前阶段只能返回覆盖结果，不会即兴编写报告。"
)


def enforce_m2_output_guard(
    result: object,
    output: str,
    *,
    event_sink: Callable[..., object] = emit_enterprise_event,
) -> str:
    """Block unaudited report prose while the active WorkItem is still in M2 intake."""

    work = _active_m2_work(result)
    if work is None:
        return output
    latest_user_message = _latest_human_message(result)
    reason = (
        "generic_confirmation"
        if _GENERIC_CONFIRM_RE.fullmatch(latest_user_message.strip())
        else "report_body"
        if _looks_like_report_body(output)
        else ""
    )
    if not reason:
        return output
    event_sink(
        "report_output_guard",
        status="blocked",
        reason=reason,
        work_id=str(work.get("work_id") or ""),
        phase=str(work.get("current_phase") or ""),
        output_chars=len(output),
    )
    return M2_OUTPUT_BLOCKED_MESSAGE


def _active_m2_work(result: object) -> Mapping[str, object] | None:
    if not isinstance(result, Mapping):
        return None
    work = result.get("current_work")
    if not isinstance(work, Mapping):
        return None
    status = str(work.get("status") or "").strip().lower()
    phase = str(work.get("current_phase") or "").strip().lower()
    if status in _TERMINAL_STATUSES or phase != "intake":
        return None
    return {str(key): value for key, value in work.items()}


def _latest_human_message(result: object) -> str:
    if not isinstance(result, Mapping):
        return ""
    messages = result.get("messages")
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        return ""
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return _content_text(message.content)
        if isinstance(message, Mapping) and str(message.get("role") or "").lower() in {"human", "user"}:
            return _content_text(message.get("content"))
    return ""


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "\n".join(part for part in parts if part)
    if isinstance(content, BaseMessage):
        return _content_text(content.content)
    return str(content or "")


def _looks_like_report_body(output: str) -> bool:
    clean = str(output or "").strip()
    if not clean:
        return False
    if len(clean) > _MAX_M2_OUTPUT_CHARS:
        return True
    if any(phrase in clean for phrase in _REPORT_BODY_PHRASES):
        return True
    if _MARKDOWN_HEADING_RE.search(clean) or _MARKDOWN_TABLE_RE.search(clean):
        return True
    return sum(label in clean for label in _REPORT_SECTION_LABELS) >= 2
