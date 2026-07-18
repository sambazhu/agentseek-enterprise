from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from hashlib import sha256

from agentseek_enterprise.observability import emit_enterprise_event
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
_GENERIC_CONFIRM_RE = re.compile(
    r"^(?:确认|同意|批准|认可|可以|好的|好|ok|okay)[。！!\s]*$",
    re.IGNORECASE,
)
_MARKDOWN_HEADING_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s+\S")
_MARKDOWN_TABLE_RE = re.compile(r"(?m)^\s*\|?.*\|.*\n\s*\|?\s*:?-{3,}")
_REPORT_BODY_PHRASES = (
    "完整报告正文",
    "报告已编写完成",
    "以下为完整报告",
    "以下为报告正文",
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
_REPORT_BRIEF_REF_RE = re.compile(
    r"(?:report\s*brief|reportbrief|报告简报)\s*(?:v|version|第)?\s*(\d+)\s*(?:版)?",
    re.IGNORECASE,
)
_REPORT_BRIEF_WRITE_CLAIM_RE = re.compile(
    r"(?:已|已经)?(?:保存|更新|修改|修订)|(?:保存|更新|修改|修订)(?:为|到)|"
    r"输出格式.{0,12}(?:改为|更新为|设为)",
    re.IGNORECASE,
)
_REPORT_BRIEF_CONFIRMED_CLAIM_RE = re.compile(
    r"status\s*[=:：]\s*confirmed|"
    r"状态\s*[=:：]?\s*(?:confirmed|已确认)",
    re.IGNORECASE,
)
_REPORT_BRIEF_PROVISIONAL_CLAIM_RE = re.compile(
    r"status\s*[=:：]\s*provisional|"
    r"状态\s*[=:：]?\s*(?:provisional|待确认)",
    re.IGNORECASE,
)
_REPORT_BRIEF_SAVE_SUCCESS_RE = re.compile(r"ReportBrief\s+v(\d+)\s+已保存")
_REPORT_BRIEF_STATUS_SUCCESS_RE = re.compile(
    r"(?:ReportBrief\s+v|当前\s+ReportBrief[：:]\s*v)"
    r"(\d+)(?:\s+已保存)?[，,]\s*(?:status|状态)\s*[=:：]\s*(provisional|confirmed)",
    re.IGNORECASE,
)
_REPORT_BRIEF_LEDGER_TOOLS = frozenset({"save_report_brief", "get_current_work_status"})
_REPORT_OUTLINE_REF_RE = re.compile(
    r"(?:report\s*outline|reportoutline|报告提纲)\s*(?:v|version|第)?\s*(\d+)\s*(?:版)?",
    re.IGNORECASE,
)
_REPORT_OUTLINE_LEDGER_CLAIM_RE = re.compile(
    r"(?:已|已经)(?:生成|创建|保存|更新|修改|修订)|"
    r"(?:已|已经)(?:由.{0,24})?确认|"
    r"status\s*[=:：]\s*(?:provisional|confirmed)|"
    r"状态\s*[=:：]?\s*(?:provisional|confirmed|待确认|已确认)",
    re.IGNORECASE,
)
_REPORT_OUTLINE_CONFIRMED_CLAIM_RE = re.compile(
    r"(?:已|已经)(?:由.{0,24})?确认|status\s*[=:：]\s*confirmed|"
    r"状态\s*[=:：]?\s*(?:confirmed|已确认)",
    re.IGNORECASE,
)
_REPORT_OUTLINE_PROVISIONAL_CLAIM_RE = re.compile(
    r"status\s*[=:：]\s*provisional|"
    r"状态\s*[=:：]?\s*(?:provisional|待确认)",
    re.IGNORECASE,
)
_REPORT_OUTLINE_STATUS_SUCCESS_RE = re.compile(
    r"(?:ReportOutline\s+v|当前\s+ReportOutline[：:]\s*v)"
    r"(\d+)[，,]\s*status=(provisional|confirmed)",
    re.IGNORECASE,
)
_REPORT_OUTLINE_CONFIRM_SUCCESS_RE = re.compile(
    r"ReportOutline\s+v(\d+)\s+已由任务委派人确认",
    re.IGNORECASE,
)
_REPORT_OUTLINE_LEDGER_TOOLS = frozenset({
    "build_report_outline",
    "get_current_report_outline",
    "get_current_work_status",
    "confirm_report_outline",
})

M2_OUTPUT_BLOCKED_MESSAGE = (
    "当前任务仍处于 M2 需求确认、内部研究和来源登记阶段，尚未启用报告正文生成。"
    "这次模型输出已被运行时守卫拦截，不作为报告或事实交付。"
    "请明确回复“确认 ReportBrief vN”，或从 get_report_research_gaps 返回的精确版本选项中选择一项。"
    "如果内部研究已无缺口，当前阶段只能返回覆盖结果，不会即兴编写报告。"
)
REPORT_BRIEF_LEDGER_CLAIM_BLOCKED_MESSAGE = (
    "未检测到本轮 save_report_brief 的成功账本写入，因此不能声称 ReportBrief "
    "已保存、修订或更改输出格式。当前账本版本保持不变；请重新调用保存工具，"
    "并以工具返回的版本为准。"
)
REPORT_OUTLINE_LEDGER_CLAIM_BLOCKED_MESSAGE = (
    "未检测到本轮 ReportOutline 工具返回的匹配账本状态，因此不能声称报告提纲已生成、"
    "保存或确认。当前提纲账本保持不变；请调用 build_report_outline、"
    "get_current_report_outline、get_current_work_status 或 confirm_report_outline，"
    "并以工具返回的版本和状态为准。"
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
    signals = _output_shape_signals(output)
    tool_sequence = _tool_call_sequence(result)
    reason = (
        "generic_confirmation"
        if _GENERIC_CONFIRM_RE.fullmatch(latest_user_message.strip())
        else "report_body"
        if _looks_like_report_body(output, signals=signals)
        else "unverified_report_brief_write"
        if _claims_unverified_report_brief_write(result, output)
        else "unverified_report_outline_write"
        if _claims_unverified_report_outline_write(result, output)
        else ""
    )
    _emit_guard_event(
        event_sink,
        status="blocked" if reason else "allowed",
        reason=reason or "operational_response",
        work=work,
        output=output,
        signals=signals,
        tool_sequence=tool_sequence,
    )
    if reason == "unverified_report_brief_write":
        return REPORT_BRIEF_LEDGER_CLAIM_BLOCKED_MESSAGE
    if reason == "unverified_report_outline_write":
        return REPORT_OUTLINE_LEDGER_CLAIM_BLOCKED_MESSAGE
    return M2_OUTPUT_BLOCKED_MESSAGE if reason else output


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
    explicit = result.get("latest_user_message")
    if isinstance(explicit, str) and explicit.strip():
        return explicit
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


def _looks_like_report_body(output: str, *, signals: Sequence[str] | None = None) -> bool:
    clean = str(output or "").strip()
    if not clean:
        return False
    if len(clean) > _MAX_M2_OUTPUT_CHARS:
        return True
    current_signals = tuple(signals or _output_shape_signals(clean))
    return any(signal.startswith("report_phrase:") for signal in current_signals)


def _output_shape_signals(output: str) -> tuple[str, ...]:
    clean = str(output or "").strip()
    signals: list[str] = []
    if len(clean) > _MAX_M2_OUTPUT_CHARS:
        signals.append("over_length_limit")
    signals.extend(
        f"report_phrase:{phrase}"
        for phrase in _REPORT_BODY_PHRASES
        if phrase in clean
    )
    if _MARKDOWN_HEADING_RE.search(clean):
        signals.append("markdown_heading")
    if _MARKDOWN_TABLE_RE.search(clean):
        signals.append("markdown_table")
    section_count = sum(label in clean for label in _REPORT_SECTION_LABELS)
    if section_count:
        signals.append(f"section_labels:{section_count}")
    return tuple(signals)


def _tool_call_sequence(result: object) -> tuple[str, ...]:
    if not isinstance(result, Mapping):
        return ()
    messages = result.get("messages")
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        return ()
    names: list[str] = []
    for message in messages:
        if _is_human_message(message):
            names.clear()
            continue
        raw_calls = _message_tool_calls(message)
        if not isinstance(raw_calls, Sequence) or isinstance(raw_calls, (str, bytes)):
            continue
        for call in raw_calls:
            if isinstance(call, Mapping):
                name = str(call.get("name") or "").strip()
                if name:
                    names.append(name[:128])
    return tuple(names[:32])


def _is_human_message(message: object) -> bool:
    if isinstance(message, HumanMessage):
        return True
    if not isinstance(message, Mapping):
        return False
    return str(message.get("role") or message.get("type") or "").lower() in {"human", "user"}


def _message_tool_calls(message: object) -> object:
    if isinstance(message, AIMessage):
        return message.tool_calls
    if isinstance(message, Mapping):
        return message.get("tool_calls", ())
    return ()


def _claims_unverified_report_brief_write(result: object, output: str) -> bool:
    claims = _report_brief_claims(output)
    if not claims:
        return False
    ledger_states = _successful_report_brief_states(result)
    return any(
        version not in ledger_states
        or (
            required_status in {"provisional", "confirmed"}
            and required_status not in ledger_states[version]
        )
        for version, required_status in claims
    )


def _report_brief_claims(output: str) -> tuple[tuple[int, str], ...]:
    matches = tuple(_REPORT_BRIEF_REF_RE.finditer(output))
    claims: list[tuple[int, str]] = []
    for index, match in enumerate(matches):
        start = max(0, match.start() - 32)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(output)
        segment = output[start:end]
        if not _REPORT_BRIEF_WRITE_CLAIM_RE.search(segment):
            continue
        status = (
            "confirmed"
            if _REPORT_BRIEF_CONFIRMED_CLAIM_RE.search(segment)
            else "provisional"
            if _REPORT_BRIEF_PROVISIONAL_CLAIM_RE.search(segment)
            else "recorded"
        )
        claims.append((int(match.group(1)), status))
    return tuple(claims)


def _successful_report_brief_states(result: object) -> dict[int, set[str]]:
    if not isinstance(result, Mapping):
        return {}
    messages = result.get("messages")
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        return {}
    ledger_call_ids: set[str] = set()
    states: dict[int, set[str]] = {}
    for message in messages:
        if _is_human_message(message):
            ledger_call_ids.clear()
            states.clear()
            continue
        ledger_call_ids.update(_report_brief_call_ids(message))
        tool_name, tool_call_id, content = _tool_result_parts(message)
        if tool_name not in _REPORT_BRIEF_LEDGER_TOOLS and tool_call_id not in ledger_call_ids:
            continue
        for version in _REPORT_BRIEF_SAVE_SUCCESS_RE.findall(content):
            states.setdefault(int(version), set()).add("recorded")
        for version, status in _REPORT_BRIEF_STATUS_SUCCESS_RE.findall(content):
            states.setdefault(int(version), set()).add(status.lower())
    return states


def _report_brief_call_ids(message: object) -> tuple[str, ...]:
    raw_calls = _message_tool_calls(message)
    if not isinstance(raw_calls, Sequence) or isinstance(raw_calls, (str, bytes)):
        return ()
    return tuple(
        call_id
        for call in raw_calls
        if isinstance(call, Mapping)
        and str(call.get("name") or "") in _REPORT_BRIEF_LEDGER_TOOLS
        and (call_id := str(call.get("id") or "").strip())
    )


def _claims_unverified_report_outline_write(result: object, output: str) -> bool:
    claims = _report_outline_claims(output)
    if not claims:
        return False
    ledger_states = _successful_report_outline_states(result)
    return any(
        version not in ledger_states
        or (
            required_status in {"provisional", "confirmed"}
            and required_status not in ledger_states[version]
        )
        for version, required_status in claims
    )


def _report_outline_claims(output: str) -> tuple[tuple[int, str], ...]:
    matches = tuple(_REPORT_OUTLINE_REF_RE.finditer(output))
    claims: list[tuple[int, str]] = []
    for index, match in enumerate(matches):
        start = max(0, match.start() - 32)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(output)
        segment = output[start:end]
        if not _REPORT_OUTLINE_LEDGER_CLAIM_RE.search(segment):
            continue
        status = (
            "confirmed"
            if _REPORT_OUTLINE_CONFIRMED_CLAIM_RE.search(segment)
            else "provisional"
            if _REPORT_OUTLINE_PROVISIONAL_CLAIM_RE.search(segment)
            else "recorded"
        )
        claims.append((int(match.group(1)), status))
    return tuple(claims)


def _successful_report_outline_states(result: object) -> dict[int, set[str]]:
    if not isinstance(result, Mapping):
        return {}
    messages = result.get("messages")
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        return {}
    outline_call_ids: set[str] = set()
    states: dict[int, set[str]] = {}
    for message in messages:
        if _is_human_message(message):
            outline_call_ids.clear()
            states.clear()
            continue
        outline_call_ids.update(_report_outline_call_ids(message))
        tool_name, tool_call_id, content = _tool_result_parts(message)
        if tool_name not in _REPORT_OUTLINE_LEDGER_TOOLS and tool_call_id not in outline_call_ids:
            continue
        _record_report_outline_states(states, content)
    return states


def _report_outline_call_ids(message: object) -> tuple[str, ...]:
    raw_calls = _message_tool_calls(message)
    if not isinstance(raw_calls, Sequence) or isinstance(raw_calls, (str, bytes)):
        return ()
    return tuple(
        call_id
        for call in raw_calls
        if isinstance(call, Mapping)
        and str(call.get("name") or "") in _REPORT_OUTLINE_LEDGER_TOOLS
        and (call_id := str(call.get("id") or "").strip())
    )


def _record_report_outline_states(states: dict[int, set[str]], content: str) -> None:
    for version, status in _REPORT_OUTLINE_STATUS_SUCCESS_RE.findall(content):
        states.setdefault(int(version), set()).add(status.lower())
    for version in _REPORT_OUTLINE_CONFIRM_SUCCESS_RE.findall(content):
        states.setdefault(int(version), set()).add("confirmed")


def _tool_result_parts(message: object) -> tuple[str, str, str]:
    if isinstance(message, ToolMessage):
        return str(message.name or ""), str(message.tool_call_id or ""), _content_text(message.content)
    if not isinstance(message, Mapping):
        return "", "", ""
    role = str(message.get("role") or message.get("type") or "").lower()
    if role != "tool":
        return "", "", ""
    return (
        str(message.get("name") or ""),
        str(message.get("tool_call_id") or ""),
        _content_text(message.get("content")),
    )


def _emit_guard_event(
    event_sink: Callable[..., object],
    *,
    status: str,
    reason: str,
    work: Mapping[str, object],
    output: str,
    signals: Sequence[str],
    tool_sequence: Sequence[str],
) -> None:
    try:
        event_sink(
            "report_output_guard",
            status=status,
            reason=reason,
            work_id=str(work.get("work_id") or ""),
            phase=str(work.get("current_phase") or ""),
            output_chars=len(output),
            output_lines=len(output.splitlines()),
            output_digest=f"sha256:{sha256(output.encode('utf-8')).hexdigest()}",
            diagnostic_signals=list(signals),
            tool_sequence=list(tool_sequence),
        )
    except Exception:
        # Delivery guards must fail open on observability errors; event emission
        # can never turn a valid employee response into a failed turn.
        return
