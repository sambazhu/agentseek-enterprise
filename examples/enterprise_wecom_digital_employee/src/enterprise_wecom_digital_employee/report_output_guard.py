from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from hashlib import sha256

from agentseek_enterprise.observability import emit_enterprise_event
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from enterprise_wecom_digital_employee.report_draft import (
    REPORT_DRAFT_MARKDOWN_BEGIN,
    REPORT_DRAFT_MARKDOWN_END,
)

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
    r"(?:已|已经)?(?:保存|暂存|更新|修改|修订)|(?:保存|暂存|更新|修改|修订)(?:为|到)|"
    r"输出格式.{0,12}(?:改为|更新为|设为)",
    re.IGNORECASE,
)
_REPORT_BRIEF_CONFIRMED_CLAIM_RE = re.compile(
    r"\bconfirmed\b|已确认|"
    r"状态\s*[=:：]?\s*(?:confirmed|已确认)",
    re.IGNORECASE,
)
_REPORT_BRIEF_PROVISIONAL_CLAIM_RE = re.compile(
    r"\bprovisional\b|待确认|暂存|"
    r"状态\s*[=:：]?\s*(?:provisional|待确认|暂存)",
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
    r"(?:已|已经)(?:生成|构建|创建|保存|更新|修改|修订)|"
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
    r"\bprovisional\b|待确认|暂定|"
    r"状态\s*[=:：]?\s*(?:provisional|待确认|暂定)",
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
_REPORT_DRAFT_REF_RE = re.compile(
    r"(?:report\s*draft|reportdraft|报告初稿)\s*(?:v|version|第)?\s*(\d+)\s*(?:版)?",
    re.IGNORECASE,
)
_REPORT_DRAFT_LEDGER_CLAIM_RE = re.compile(
    r"(?:已|已经)(?:生成|构建|创建|保存|更新|修改|修订)|"
    r"(?:已|已经)(?:由.{0,24})?确认|"
    r"status\s*[=:：]\s*(?:provisional|confirmed)|"
    r"状态\s*[=:：]?\s*(?:provisional|confirmed|待确认|已确认)",
    re.IGNORECASE,
)
_REPORT_DRAFT_CONFIRMED_CLAIM_RE = re.compile(
    r"(?:已|已经)(?:由.{0,24})?确认|status\s*[=:：]\s*confirmed|"
    r"状态\s*[=:：]?\s*(?:confirmed|已确认)",
    re.IGNORECASE,
)
_REPORT_DRAFT_PROVISIONAL_CLAIM_RE = re.compile(
    r"\bprovisional\b|待确认|暂存|暂定|"
    r"状态\s*[=:：]?\s*(?:provisional|待确认|暂存|暂定)",
    re.IGNORECASE,
)
_REPORT_DRAFT_STATUS_SUCCESS_RE = re.compile(
    r"(?:ReportDraft\s+v|当前\s+ReportDraft[：:]\s*v)"
    r"(\d+)[，,]\s*status=(provisional|confirmed)",
    re.IGNORECASE,
)
_REPORT_DRAFT_CONFIRM_SUCCESS_RE = re.compile(
    r"ReportDraft\s+v(\d+)\s+已由任务委派人确认",
    re.IGNORECASE,
)
_REPORT_DRAFT_LEDGER_TOOLS = frozenset({
    "build_report_draft",
    "get_current_report_draft",
    "get_current_work_status",
    "confirm_report_draft",
})
_REPORT_APPROVAL_REF_RE = _REPORT_DRAFT_REF_RE
_REPORT_APPROVAL_CLAIM_RE = re.compile(
    r"(?:已|已经)(?:提交|申请|发起|进入).{0,12}(?:审批|批准)|送审|待审批|"
    r"(?:已|已经)(?:审批通过|批准)|approval[_\s-]*state\s*[=:：]\s*(?:pending|approved)|"
    r"\b(?:pending\s+approval|approved)\b",
    re.IGNORECASE,
)
_REPORT_APPROVAL_APPROVED_CLAIM_RE = re.compile(
    r"(?:已|已经)(?:审批通过|批准)|approval[_\s-]*state\s*[=:：]\s*approved|"
    r"status\s*[=:：]\s*approved|\bapproved\b",
    re.IGNORECASE,
)
_REPORT_APPROVAL_PENDING_CLAIM_RE = re.compile(
    r"待审批|(?:已|已经)(?:提交|申请|发起|进入).{0,12}(?:审批|批准)|送审|"
    r"approval[_\s-]*state\s*[=:：]\s*pending|status\s*[=:：]\s*pending|"
    r"\bpending\s+approval\b",
    re.IGNORECASE,
)
_REPORT_APPROVAL_STATUS_SUCCESS_RE = re.compile(
    r"ReportApproval\s+contract_v\d+[，,]\s*status=(pending|approved)[，,]"
    r"\s*bound_report_draft_v(\d+)",
    re.IGNORECASE,
)
_REPORT_APPROVAL_LEDGER_TOOLS = frozenset({
    "request_report_approval",
    "get_current_report_approval",
    "get_current_work_status",
    "approve_report_draft",
})
_REPORT_ARTIFACT_CLAIM_RE = re.compile(
    r"(?:ReportArtifact|artifact_id=|DOCX\s*(?:Artifact|文件|文档)?).{0,40}"
    r"(?:已|已经)(?:生成|渲染|创建|登记)|"
    r"(?:已|已经)(?:生成|渲染|创建|登记).{0,40}(?:ReportArtifact|DOCX|artifact_id=)",
    re.IGNORECASE,
)
_REPORT_ARTIFACT_ID_RE = re.compile(r"artifact_id=([A-Za-z0-9_-]+)", re.IGNORECASE)
_REPORT_ARTIFACT_DRAFT_VERSION_RE = re.compile(r"bound_report_draft_v(\d+)", re.IGNORECASE)
_REPORT_ARTIFACT_STATUS_SUCCESS_RE = re.compile(
    r"ReportArtifact\s+artifact_id=([A-Za-z0-9_-]+)[，,]\s*format=(docx)[，,]"
    r"\s*bound_report_draft_v(\d+).{0,240}?current=(true|false)",
    re.IGNORECASE,
)
_REPORT_ARTIFACT_LEDGER_TOOLS = frozenset({
    "render_report_docx_artifact",
    "get_current_report_artifacts",
    "get_current_work_status",
})
_REPORT_PUBLICATION_REF_RE = re.compile(
    r"report\s*artifact\s*(?:v|version|第)?\s*(\d+)\s*(?:版)?|"
    r"bound_report_draft_v(\d+)",
    re.IGNORECASE,
)
_REPORT_PUBLICATION_CLAIM_RE = re.compile(
    r"(?:已|已经)(?:正式)?发布|status\s*[=:：]\s*published|"
    r"publication\s*[=:：]\s*published|(?:当前|现行)(?:正式)?发布版本|"
    r"发布版本.{0,8}(?:当前|现行)",
    re.IGNORECASE,
)
_REPORT_PUBLICATION_CURRENT_RE = re.compile(
    r"current\s*[=:：]\s*true|(?:仍是|仍为|属于)?(?:当前|现行)(?:正式)?发布版本|"
    r"发布版本.{0,8}(?:当前|现行)",
    re.IGNORECASE,
)
_REPORT_PUBLICATION_STALE_RE = re.compile(
    r"current\s*[=:：]\s*false|(?:历史|旧版|已失效|非当前).{0,12}(?:发布|版本)",
    re.IGNORECASE,
)
_REPORT_PUBLICATION_STATUS_SUCCESS_RE = re.compile(
    r"ReportPublication\s+publication_id=([A-Za-z0-9_-]+).{0,160}?"
    r"status=published.{0,160}?bound_report_draft_v(\d+).{0,240}?current=(true|false)",
    re.IGNORECASE,
)
_REPORT_PUBLICATION_LEDGER_TOOLS = frozenset({
    "publish_report_artifact",
    "get_current_report_publications",
    "get_current_work_status",
})
_REPORT_DELIVERY_CLAIM_RE = re.compile(
    r"(?:报告|ReportArtifact|DOCX).{0,36}(?:已|已经)(?:交付|发送|下载)|"
    r"(?:delivery|status)\s*[=:：]\s*delivered|(?:已|已经)(?:交付|发送|下载)",
    re.IGNORECASE,
)
_REPORT_DELIVERY_REF_RE = re.compile(
    r"report\s*artifact\s*(?:v|version|第)?\s*(\d+)\s*(?:版)?|"
    r"bound_report_draft_v(\d+)",
    re.IGNORECASE,
)
_REPORT_DELIVERY_STATUS_SUCCESS_RE = re.compile(
    r"ReportDelivery\s+delivery_(?:id=[A-Za-z0-9_-]+|v\d+).{0,240}?"
    r"status=delivered.{0,240}?bound_report_draft_v(\d+).{0,240}?current=(true|false)",
    re.IGNORECASE,
)
_REPORT_DELIVERY_LEDGER_TOOLS = frozenset({
    "deliver_report_artifact",
    "get_current_report_deliveries",
    "get_current_work_status",
})

M2_OUTPUT_BLOCKED_MESSAGE = (
    "未检测到本轮账本支持的 ReportDraft，因此这次模型正文已被运行时守卫拦截，"
    "不作为报告或事实交付。请先确认准确的 ReportOutline，再调用 "
    "prepare_report_draft_context 和 build_report_draft；DOCX/PDF 与最终批准尚未启用。"
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
REPORT_DRAFT_LEDGER_CLAIM_BLOCKED_MESSAGE = (
    "未检测到匹配的 ReportDraft 账本状态，因此不能声称报告初稿已生成、保存或确认。"
    "当前初稿账本保持不变；请调用 build_report_draft、get_current_report_draft、"
    "confirm_report_draft 或 "
    "get_current_work_status，并以工具返回的版本和 Markdown 为准。"
)
REPORT_APPROVAL_LEDGER_CLAIM_BLOCKED_MESSAGE = (
    "未检测到匹配的 ReportApproval 账本状态，因此不能声称 ReportDraft 已提交审批或已批准。"
    "请调用 request_report_approval、get_current_report_approval、get_current_work_status "
    "或 approve_report_draft，并以工具返回的精确 Draft 版本和审批状态为准。"
    "批准不等于发布、渲染或交付。"
)
REPORT_ARTIFACT_LEDGER_CLAIM_BLOCKED_MESSAGE = (
    "未检测到匹配的 ReportArtifact 账本记录，因此不能声称 DOCX 已生成、已发布或已交付。"
    "请调用 render_report_docx_artifact、get_current_report_artifacts 或 get_current_work_status，"
    "并以工具返回的 artifact_id、Draft 版本和 current 状态为准。渲染不等于发布或交付。"
)
REPORT_PUBLICATION_LEDGER_CLAIM_BLOCKED_MESSAGE = (
    "未检测到匹配的当前 ReportPublication 账本记录，因此不能声称 ReportArtifact 已正式发布。"
    "请调用 publish_report_artifact、get_current_report_publications 或 get_current_work_status，"
    "并以工具返回的发布版本、Artifact 绑定和 current 状态为准。发布不等于交付或发送。"
)
REPORT_DELIVERY_LEDGER_CLAIM_BLOCKED_MESSAGE = (
    "未检测到匹配的当前 ReportDelivery 账本记录，因此不能声称文件已交付、"
    "已发送或已下载。请调用 deliver_report_artifact、get_current_report_deliveries "
    "或 get_current_work_status，并以工具返回的 Artifact 绑定和 current 状态为准。"
)
_OUTLINE_CONFIRMATION_NUDGE = "提纲已确认；如需初稿，请另行回复“生成可审阅初稿”。"


def enforce_m2_output_guard(  # noqa: C901
    result: object,
    output: str,
    *,
    event_sink: Callable[..., object] = emit_enterprise_event,
) -> str:
    """Allow only ledger-backed report prose for an active report WorkItem."""

    work = _active_m2_work(result)
    if work is None:
        return output
    latest_user_message = _latest_human_message(result)
    signals = _output_shape_signals(output)
    tool_sequence = _tool_call_sequence(result)
    verified_draft_output = _successful_report_draft_output(result)
    reason = (
        "generic_confirmation"
        if _GENERIC_CONFIRM_RE.fullmatch(latest_user_message.strip())
        else "ledger_backed_report_draft"
        if verified_draft_output
        else "report_body"
        if _looks_like_report_body(output, signals=signals)
        else "unverified_report_brief_write"
        if _claims_unverified_report_brief_write(result, output)
        else "unverified_report_outline_write"
        if _claims_unverified_report_outline_write(result, output)
        else "unverified_report_delivery"
        if _claims_unverified_report_delivery(result, output)
        else "unverified_report_publication"
        if _claims_unverified_report_publication(result, output)
        else "unverified_report_artifact"
        if _claims_unverified_report_artifact(result, output)
        else "unverified_report_draft_write"
        if _claims_unverified_report_draft_write(result, output)
        else "unverified_report_approval"
        if _claims_unverified_report_approval(result, output)
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
    if reason == "unverified_report_draft_write":
        return REPORT_DRAFT_LEDGER_CLAIM_BLOCKED_MESSAGE
    if reason == "unverified_report_approval":
        return REPORT_APPROVAL_LEDGER_CLAIM_BLOCKED_MESSAGE
    if reason == "unverified_report_artifact":
        return REPORT_ARTIFACT_LEDGER_CLAIM_BLOCKED_MESSAGE
    if reason == "unverified_report_publication":
        return REPORT_PUBLICATION_LEDGER_CLAIM_BLOCKED_MESSAGE
    if reason == "unverified_report_delivery":
        return REPORT_DELIVERY_LEDGER_CLAIM_BLOCKED_MESSAGE
    if reason == "ledger_backed_report_draft":
        return verified_draft_output
    if reason:
        return M2_OUTPUT_BLOCKED_MESSAGE
    return _append_outline_confirmation_nudge(result, output)


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
    for match in matches:
        segment = _claim_line(output, match)
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
    states: dict[int, set[str]] = {}
    messages = result.get("messages")
    ledger_call_ids: set[str] = set()
    if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes)):
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
    _merge_current_work_contract_state(result, states, "report_brief")
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
    for match in matches:
        segment = _claim_line(output, match)
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


def _claim_line(output: str, match: re.Match[str]) -> str:
    """Return only the current contract line so adjacent statuses cannot leak in."""

    start = output.rfind("\n", 0, match.start()) + 1
    end = output.find("\n", match.end())
    return output[start:] if end < 0 else output[start:end]


def _successful_report_outline_states(result: object) -> dict[int, set[str]]:
    if not isinstance(result, Mapping):
        return {}
    states: dict[int, set[str]] = {}
    messages = result.get("messages")
    outline_call_ids: set[str] = set()
    if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes)):
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
    _merge_current_work_contract_state(result, states, "report_outline")
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


def _claims_unverified_report_draft_write(result: object, output: str) -> bool:
    claims = _report_draft_claims(output)
    if not claims:
        return False
    ledger_states = _successful_report_draft_states(result)
    return any(
        version not in ledger_states
        or (
            required_status in {"provisional", "confirmed"}
            and required_status not in ledger_states[version]
        )
        for version, required_status in claims
    )


def _claims_unverified_report_approval(result: object, output: str) -> bool:
    claims = _report_approval_claims(output)
    if not claims:
        return False
    ledger_states = _successful_report_approval_states(result)
    return any(status not in ledger_states.get(version, set()) for version, status in claims)


def _report_approval_claims(output: str) -> tuple[tuple[int, str], ...]:
    claims: list[tuple[int, str]] = []
    for match in _REPORT_APPROVAL_REF_RE.finditer(output):
        segment = _claim_line(output, match)
        if not _REPORT_APPROVAL_CLAIM_RE.search(segment):
            continue
        status = (
            "approved"
            if _REPORT_APPROVAL_APPROVED_CLAIM_RE.search(segment)
            else "pending"
            if _REPORT_APPROVAL_PENDING_CLAIM_RE.search(segment)
            else ""
        )
        if status:
            claims.append((int(match.group(1)), status))
    return tuple(claims)


def _successful_report_approval_states(result: object) -> dict[int, set[str]]:
    if not isinstance(result, Mapping):
        return {}
    states: dict[int, set[str]] = {}
    messages = result.get("messages")
    call_ids: set[str] = set()
    if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes)):
        for message in messages:
            if _is_human_message(message):
                call_ids.clear()
                states.clear()
                continue
            call_ids.update(_tool_call_ids(message, _REPORT_APPROVAL_LEDGER_TOOLS))
            tool_name, tool_call_id, content = _tool_result_parts(message)
            if tool_name not in _REPORT_APPROVAL_LEDGER_TOOLS and tool_call_id not in call_ids:
                continue
            for status, draft_version in _REPORT_APPROVAL_STATUS_SUCCESS_RE.findall(content):
                states.setdefault(int(draft_version), set()).add(status.lower())
    if isinstance(work := result.get("current_work"), Mapping):
        approval = work.get("report_approval")
        if isinstance(approval, Mapping) and approval.get("current") is True:
            version = approval.get("report_draft_version")
            status = str(approval.get("status") or "").lower()
            if isinstance(version, int) and not isinstance(version, bool) and status in {"pending", "approved"}:
                states.setdefault(version, set()).add(status)
    return states


def _claims_unverified_report_artifact(result: object, output: str) -> bool:
    if not _REPORT_ARTIFACT_CLAIM_RE.search(output):
        return False
    artifact_ids = set(_REPORT_ARTIFACT_ID_RE.findall(output))
    draft_versions = {int(value) for value in _REPORT_ARTIFACT_DRAFT_VERSION_RE.findall(output)}
    ledger_ids, ledger_versions = _successful_report_artifact_states(result)
    if artifact_ids:
        return not artifact_ids.issubset(ledger_ids)
    if draft_versions:
        return not draft_versions.issubset(ledger_versions)
    return not ledger_ids


def _claims_unverified_report_delivery(result: object, output: str) -> bool:
    if not _REPORT_DELIVERY_CLAIM_RE.search(output):
        return False
    claims = {
        int(first or second)
        for first, second in _REPORT_DELIVERY_REF_RE.findall(output)
        if first or second
    }
    states = _successful_report_delivery_states(result)
    if not claims:
        return not any(True in values for values in states.values())
    return any(True not in states.get(version, set()) for version in claims)


def _successful_report_delivery_states(result: object) -> dict[int, set[bool]]:  # noqa: C901
    if not isinstance(result, Mapping):
        return {}
    states: dict[int, set[bool]] = {}
    call_ids: set[str] = set()
    messages = result.get("messages")
    if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes)):
        for message in messages:
            if _is_human_message(message):
                call_ids.clear()
                states.clear()
                continue
            call_ids.update(_tool_call_ids(message, _REPORT_DELIVERY_LEDGER_TOOLS))
            tool_name, tool_call_id, content = _tool_result_parts(message)
            if tool_name not in _REPORT_DELIVERY_LEDGER_TOOLS and tool_call_id not in call_ids:
                continue
            for version, current in _REPORT_DELIVERY_STATUS_SUCCESS_RE.findall(content):
                states.setdefault(int(version), set()).add(current.lower() == "true")
    work = result.get("current_work")
    if isinstance(work, Mapping):
        deliveries = work.get("report_deliveries")
        if isinstance(deliveries, Sequence) and not isinstance(deliveries, (str, bytes)):
            for delivery in deliveries:
                if not isinstance(delivery, Mapping):
                    continue
                version = delivery.get("report_draft_version")
                if isinstance(version, int) and not isinstance(version, bool):
                    states.setdefault(version, set()).add(delivery.get("current") is True)
    return states


def _successful_report_artifact_states(result: object) -> tuple[set[str], set[int]]:
    if not isinstance(result, Mapping):
        return set(), set()
    artifact_ids, draft_versions = _artifact_states_from_messages(result.get("messages"))
    work_ids, work_versions = _artifact_states_from_work(result.get("current_work"))
    return artifact_ids | work_ids, draft_versions | work_versions


def _artifact_states_from_messages(messages: object) -> tuple[set[str], set[int]]:
    artifact_ids: set[str] = set()
    draft_versions: set[int] = set()
    call_ids: set[str] = set()
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        return artifact_ids, draft_versions
    for message in messages:
        if _is_human_message(message):
            call_ids.clear()
            artifact_ids.clear()
            draft_versions.clear()
            continue
        call_ids.update(_tool_call_ids(message, _REPORT_ARTIFACT_LEDGER_TOOLS))
        tool_name, tool_call_id, content = _tool_result_parts(message)
        if tool_name not in _REPORT_ARTIFACT_LEDGER_TOOLS and tool_call_id not in call_ids:
            continue
        for artifact_id, artifact_format, draft_version, _current in _REPORT_ARTIFACT_STATUS_SUCCESS_RE.findall(
            content
        ):
            if artifact_format.lower() == "docx":
                artifact_ids.add(artifact_id)
                draft_versions.add(int(draft_version))
    return artifact_ids, draft_versions


def _claims_unverified_report_publication(result: object, output: str) -> bool:
    if not _REPORT_PUBLICATION_CLAIM_RE.search(output):
        return False
    if any(
        _REPORT_PUBLICATION_STALE_RE.search(line)
        and _REPORT_PUBLICATION_CURRENT_RE.search(line)
        for line in output.splitlines()
    ):
        return True
    claims = _report_publication_claims(output)
    states = _successful_report_publication_states(result)
    if not claims:
        return not any(True in values for values in states.values())
    return any(current not in states.get(version, set()) for version, current in claims)


def _report_publication_claims(output: str) -> tuple[tuple[int, bool], ...]:
    claims: list[tuple[int, bool]] = []
    for match in _REPORT_PUBLICATION_REF_RE.finditer(output):
        line = _claim_line(output, match)
        if not _REPORT_PUBLICATION_CLAIM_RE.search(line):
            continue
        version_text = match.group(1) or match.group(2)
        current = bool(_REPORT_PUBLICATION_CURRENT_RE.search(line)) or not bool(
            _REPORT_PUBLICATION_STALE_RE.search(line)
        )
        claims.append((int(version_text), current))
    return tuple(claims)


def _successful_report_publication_states(result: object) -> dict[int, set[bool]]:
    if not isinstance(result, Mapping):
        return {}
    states = _publication_states_from_messages(result.get("messages"))
    _merge_current_work_publication_states(result.get("current_work"), states)
    return states


def _publication_states_from_messages(messages: object) -> dict[int, set[bool]]:
    states: dict[int, set[bool]] = {}
    call_ids: set[str] = set()
    if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes)):
        for message in messages:
            if _is_human_message(message):
                call_ids.clear()
                states.clear()
                continue
            call_ids.update(_tool_call_ids(message, _REPORT_PUBLICATION_LEDGER_TOOLS))
            tool_name, tool_call_id, content = _tool_result_parts(message)
            if tool_name not in _REPORT_PUBLICATION_LEDGER_TOOLS and tool_call_id not in call_ids:
                continue
            for _publication_id, version, current in _REPORT_PUBLICATION_STATUS_SUCCESS_RE.findall(content):
                states.setdefault(int(version), set()).add(current.lower() == "true")
    return states


def _merge_current_work_publication_states(work: object, states: dict[int, set[bool]]) -> None:
    if not isinstance(work, Mapping):
        return
    publications = work.get("report_publications")
    if not isinstance(publications, Sequence) or isinstance(publications, (str, bytes)):
        return
    for publication in publications:
        if not isinstance(publication, Mapping):
            continue
        version = publication.get("report_draft_version")
        if isinstance(version, int) and not isinstance(version, bool):
            states.setdefault(version, set()).add(publication.get("current") is True)


def _artifact_states_from_work(work: object) -> tuple[set[str], set[int]]:
    artifact_ids: set[str] = set()
    draft_versions: set[int] = set()
    if not isinstance(work, Mapping):
        return artifact_ids, draft_versions
    artifacts = work.get("report_artifacts")
    if not isinstance(artifacts, Sequence) or isinstance(artifacts, (str, bytes)):
        return artifact_ids, draft_versions
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            continue
        artifact_id = str(artifact.get("artifact_id") or "").strip()
        draft_version = artifact.get("report_draft_version")
        if artifact_id:
            artifact_ids.add(artifact_id)
        if isinstance(draft_version, int) and not isinstance(draft_version, bool):
            draft_versions.add(draft_version)
    return artifact_ids, draft_versions


def _append_outline_confirmation_nudge(result: object, output: str) -> str:
    if "生成可审阅初稿" in output or not _successful_outline_confirmation_in_turn(result):
        return output
    return f"{output.rstrip()}\n\n{_OUTLINE_CONFIRMATION_NUDGE}"


def _successful_outline_confirmation_in_turn(result: object) -> bool:
    if not isinstance(result, Mapping):
        return False
    messages = result.get("messages")
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        return False
    call_ids: set[str] = set()
    succeeded = False
    for message in messages:
        if _is_human_message(message):
            call_ids.clear()
            succeeded = False
            continue
        call_ids.update(_tool_call_ids(message, frozenset({"confirm_report_outline"})))
        tool_name, tool_call_id, content = _tool_result_parts(message)
        if tool_name == "confirm_report_outline" or tool_call_id in call_ids:
            succeeded = bool(_REPORT_OUTLINE_CONFIRM_SUCCESS_RE.search(content))
    return succeeded


def _tool_call_ids(message: object, tool_names: frozenset[str]) -> tuple[str, ...]:
    raw_calls = _message_tool_calls(message)
    if not isinstance(raw_calls, Sequence) or isinstance(raw_calls, (str, bytes)):
        return ()
    return tuple(
        call_id
        for call in raw_calls
        if isinstance(call, Mapping)
        and str(call.get("name") or "") in tool_names
        and (call_id := str(call.get("id") or "").strip())
    )


def _report_draft_claims(output: str) -> tuple[tuple[int, str], ...]:
    claims: list[tuple[int, str]] = []
    for match in _REPORT_DRAFT_REF_RE.finditer(output):
        segment = _claim_line(output, match)
        if not _REPORT_DRAFT_LEDGER_CLAIM_RE.search(segment):
            continue
        status = (
            "confirmed"
            if _REPORT_DRAFT_CONFIRMED_CLAIM_RE.search(segment)
            else "provisional"
            if _REPORT_DRAFT_PROVISIONAL_CLAIM_RE.search(segment)
            else "recorded"
        )
        claims.append((int(match.group(1)), status))
    return tuple(claims)


def _successful_report_draft_output(result: object) -> str:
    """Return the last exact ledger draft block produced by a trusted draft tool."""

    if not isinstance(result, Mapping):
        return ""
    messages = result.get("messages")
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        return ""
    call_ids: set[str] = set()
    verified = ""
    for message in messages:
        if _is_human_message(message):
            call_ids.clear()
            verified = ""
            continue
        call_ids.update(_report_draft_call_ids(message, include_status=False))
        tool_name, tool_call_id, content = _tool_result_parts(message)
        if tool_name not in {"build_report_draft", "get_current_report_draft"} and tool_call_id not in call_ids:
            continue
        if (
            _REPORT_DRAFT_STATUS_SUCCESS_RE.search(content)
            and REPORT_DRAFT_MARKDOWN_BEGIN in content
            and REPORT_DRAFT_MARKDOWN_END in content
        ):
            verified = content
    return verified


def _successful_report_draft_states(result: object) -> dict[int, set[str]]:
    if not isinstance(result, Mapping):
        return {}
    states: dict[int, set[str]] = {}
    messages = result.get("messages")
    call_ids: set[str] = set()
    if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes)):
        for message in messages:
            if _is_human_message(message):
                call_ids.clear()
                states.clear()
                continue
            call_ids.update(_report_draft_call_ids(message, include_status=True))
            tool_name, tool_call_id, content = _tool_result_parts(message)
            if tool_name not in _REPORT_DRAFT_LEDGER_TOOLS and tool_call_id not in call_ids:
                continue
            for version, status in _REPORT_DRAFT_STATUS_SUCCESS_RE.findall(content):
                states.setdefault(int(version), set()).add(status.lower())
            for version in _REPORT_DRAFT_CONFIRM_SUCCESS_RE.findall(content):
                states.setdefault(int(version), set()).add("confirmed")
    _merge_current_work_contract_state(result, states, "report_draft")
    return states


def _merge_current_work_contract_state(
    result: object,
    states: dict[int, set[str]],
    contract_key: str,
) -> None:
    """Merge only the server-published current ledger snapshot into guard proof."""

    if not isinstance(result, Mapping):
        return
    work = result.get("current_work")
    if not isinstance(work, Mapping):
        return
    contract = work.get(contract_key)
    if not isinstance(contract, Mapping):
        return
    version = contract.get("contract_version")
    status = str(contract.get("status") or "").lower()
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version <= 0
        or status not in {"provisional", "confirmed"}
    ):
        return
    states.setdefault(version, set()).update({"recorded", status})


def _report_draft_call_ids(message: object, *, include_status: bool) -> tuple[str, ...]:
    raw_calls = _message_tool_calls(message)
    if not isinstance(raw_calls, Sequence) or isinstance(raw_calls, (str, bytes)):
        return ()
    tool_names = _REPORT_DRAFT_LEDGER_TOOLS if include_status else {"build_report_draft", "get_current_report_draft"}
    return tuple(
        call_id
        for call in raw_calls
        if isinstance(call, Mapping)
        and str(call.get("name") or "") in tool_names
        and (call_id := str(call.get("id") or "").strip())
    )


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
