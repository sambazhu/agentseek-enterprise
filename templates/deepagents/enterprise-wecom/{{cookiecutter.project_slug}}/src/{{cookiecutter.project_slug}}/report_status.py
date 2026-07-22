from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import cast

from {{ cookiecutter.project_slug }}.channel_command import authenticated_user_command_text


class ReportStatusSection(StrEnum):
    SUMMARY = "business_summary"
    BRIEF = "report_brief"
    GAP_DECISION = "research_gap_decision"
    OUTLINE = "report_outline"
    DRAFT = "report_draft"
    APPROVAL = "report_approval"
    ARTIFACT = "report_artifacts"
    PUBLICATION = "report_publications"
    DELIVERY = "report_deliveries"


_DETAIL_SECTIONS = (
    ReportStatusSection.BRIEF,
    ReportStatusSection.GAP_DECISION,
    ReportStatusSection.OUTLINE,
    ReportStatusSection.DRAFT,
    ReportStatusSection.APPROVAL,
    ReportStatusSection.ARTIFACT,
    ReportStatusSection.PUBLICATION,
    ReportStatusSection.DELIVERY,
)
_READ_PREFIX_RE = re.compile(r"^(?:请)?(?:查看|查询|显示|告诉我)(?:一下)?")
_WHITESPACE_RE = re.compile(r"\s+")
_STATUS_LABELS = {
    "draft": "处理中",
    "provisional": "待确认",
    "confirmed": "已确认",
    "pending": "待审批",
    "approved": "已批准",
    "published": "已发布",
    "delivered": "已交付",
    "consumed": "已下载",
    "active": "可下载",
    "expired": "已过期",
}


def match_report_status_sections(message: str) -> tuple[ReportStatusSection, ...] | None:
    """Recognize a bounded read-only report-ledger query without a model."""

    command = _normalized_command(message)
    if not _READ_PREFIX_RE.match(command) or "当前" not in command:
        return None
    if "报告任务状态" in command or "当前任务状态" in command or "当前报告状态" in command:
        return (ReportStatusSection.SUMMARY,)

    sections: list[ReportStatusSection] = []
    _append_if(sections, ReportStatusSection.BRIEF, "reportbrief" in command)
    _append_if(sections, ReportStatusSection.GAP_DECISION, "缺口决策" in command)
    _append_if(sections, ReportStatusSection.OUTLINE, "reportoutline" in command or "报告提纲" in command)
    _append_if(sections, ReportStatusSection.DRAFT, "reportdraft" in command or "报告初稿" in command)
    _append_if(sections, ReportStatusSection.APPROVAL, "reportapproval" in command or "报告审批" in command)
    _append_if(sections, ReportStatusSection.ARTIFACT, "reportartifact" in command or "报告文件" in command)
    _append_if(
        sections,
        ReportStatusSection.PUBLICATION,
        "reportpublication" in command or "发布状态" in command or "发布交付状态" in command,
    )
    _append_if(
        sections,
        ReportStatusSection.DELIVERY,
        "reportdelivery" in command or "交付状态" in command or "发布交付状态" in command,
    )
    return tuple(sections) or None


def render_report_status(  # noqa: C901 - renders bounded ledger sections
    summary: Mapping[str, object] | None,
    *,
    sections: Sequence[ReportStatusSection] = _DETAIL_SECTIONS,
) -> str:
    """Render ledger-backed status text without model-authored business claims."""

    if summary is None:
        return "当前员工没有可见的进行中任务。"
    selected = frozenset(sections)
    if ReportStatusSection.SUMMARY in selected:
        return _render_business_summary(summary)
    lines = [
        f"当前报告任务：work_id={summary['work_id']}，"
        f"状态={_status_label(summary['status'])}，阶段={summary['current_phase']}，"
        f"服务={summary['playbook_id']}@{summary['playbook_version']}。"
    ]
    if ReportStatusSection.BRIEF in selected:
        _append_brief(lines, summary.get(ReportStatusSection.BRIEF.value))
    if ReportStatusSection.GAP_DECISION in selected:
        _append_gap_decision(lines, summary.get(ReportStatusSection.GAP_DECISION.value))
    if ReportStatusSection.OUTLINE in selected:
        _append_outline(lines, summary.get(ReportStatusSection.OUTLINE.value))
    if ReportStatusSection.DRAFT in selected:
        _append_draft(lines, summary.get(ReportStatusSection.DRAFT.value))
    if ReportStatusSection.APPROVAL in selected:
        _append_approval(lines, summary.get(ReportStatusSection.APPROVAL.value))
    if ReportStatusSection.ARTIFACT in selected:
        _append_artifacts(lines, summary.get(ReportStatusSection.ARTIFACT.value))
    if ReportStatusSection.PUBLICATION in selected:
        _append_publications(lines, summary.get(ReportStatusSection.PUBLICATION.value))
    if ReportStatusSection.DELIVERY in selected:
        _append_deliveries(lines, summary.get(ReportStatusSection.DELIVERY.value))
    return "\n\n".join(lines)


def _render_business_summary(summary: Mapping[str, object]) -> str:
    brief = summary.get(ReportStatusSection.BRIEF.value)
    outline = summary.get(ReportStatusSection.OUTLINE.value)
    draft = summary.get(ReportStatusSection.DRAFT.value)
    approval = summary.get(ReportStatusSection.APPROVAL.value)
    artifact = _current_record(summary.get(ReportStatusSection.ARTIFACT.value))
    publication = _current_record(summary.get(ReportStatusSection.PUBLICATION.value))
    delivery = _latest_current_delivery(summary.get(ReportStatusSection.DELIVERY.value))
    title = str(summary.get("report_title") or "当前证券行业报告")

    lines = ["**当前报告任务**", f"- 主题：{title}", f"- 进度：{_business_stage(summary)}"]
    if summary.get("coverage_period"):
        lines.append(f"- 报告覆盖期：{summary.get('coverage_period')}")

    completed: list[str] = []
    if isinstance(brief, Mapping) and brief.get("status") == "confirmed":
        completed.append("需求已经确认")
    if isinstance(outline, Mapping) and outline.get("status") == "confirmed":
        completed.append("研究与报告提纲已经确认")
    if isinstance(draft, Mapping) and draft.get("status") == "confirmed":
        completed.append("报告初稿已经确认")
    if isinstance(approval, Mapping) and approval.get("status") == "approved" and approval.get("current") is True:
        completed.append("报告内容已经批准")
    if publication is not None:
        completed.append("DOCX 文件已经正式发布")
    if delivery is not None:
        completed.append("下载卡片已经交付")
    if completed:
        lines.extend(("", "**已完成**", *(f"- {item}" for item in completed)))

    lines.extend(("", "**下一步**", f"- {_next_business_action(brief, outline, draft, approval, artifact, publication, delivery)}"))
    return "\n".join(lines)


def _business_stage(summary: Mapping[str, object]) -> str:
    deliveries = _mapping_records(summary.get(ReportStatusSection.DELIVERY.value))
    if any(item.get("current") is True for item in deliveries):
        return "报告已交付"
    publications = _mapping_records(summary.get(ReportStatusSection.PUBLICATION.value))
    if any(item.get("current") is True for item in publications):
        return "文件已发布，等待交付"
    artifacts = _mapping_records(summary.get(ReportStatusSection.ARTIFACT.value))
    if any(item.get("current") is True for item in artifacts):
        return "文件已生成，等待发布"
    approval = summary.get(ReportStatusSection.APPROVAL.value)
    if isinstance(approval, Mapping) and approval.get("current") is True:
        return "内容已批准" if approval.get("status") == "approved" else "等待内容审批"
    draft = summary.get(ReportStatusSection.DRAFT.value)
    if isinstance(draft, Mapping):
        return "初稿已确认" if draft.get("status") == "confirmed" else "初稿待确认"
    outline = summary.get(ReportStatusSection.OUTLINE.value)
    if isinstance(outline, Mapping):
        return "提纲已确认" if outline.get("status") == "confirmed" else "提纲待确认"
    brief = summary.get(ReportStatusSection.BRIEF.value)
    if isinstance(brief, Mapping):
        return "正在研究" if brief.get("status") == "confirmed" else "需求待确认"
    return _status_label(summary.get("status"))


def _next_business_action(  # noqa: C901 - ordered lifecycle guidance
    brief: object,
    outline: object,
    draft: object,
    approval: object,
    artifact: Mapping[str, object] | None,
    publication: Mapping[str, object] | None,
    delivery: Mapping[str, object] | None,
) -> str:
    if delivery is not None:
        version = delivery.get("report_draft_version")
        if delivery.get("grant_state") == "active":
            return "下载卡片仍在有效期内，请在企微中打开卡片下载。"
        return f"如需重新下载，请回复“交付 ReportArtifact v{version} 给我”。"
    if publication is not None:
        return f"如需获取文件，请回复“交付 ReportArtifact v{publication.get('report_draft_version')} 给我”。"
    if artifact is not None:
        return f"如需正式发布，请回复“发布 ReportArtifact v{artifact.get('report_draft_version')}”。"
    if isinstance(approval, Mapping) and approval.get("current") is True:
        version = approval.get("report_draft_version")
        if approval.get("status") == "approved":
            return f"如需生成 DOCX，请回复“生成 ReportDraft v{version} DOCX”。"
        return f"审批人如认可内容，请回复“批准 ReportDraft v{version}”。"
    if isinstance(draft, Mapping):
        version = draft.get("contract_version")
        if draft.get("status") == "confirmed":
            return f"如需进入内容审批，请回复“提交 ReportDraft v{version} 审批”。"
        return f"如认可初稿，请回复“确认 ReportDraft v{version}”。"
    if isinstance(outline, Mapping):
        version = outline.get("contract_version")
        if outline.get("status") == "confirmed":
            return "如需初稿，请回复“生成可审阅初稿”。"
        return f"如认可提纲，请回复“确认 ReportOutline v{version}”。"
    if isinstance(brief, Mapping):
        version = brief.get("contract_version")
        if brief.get("status") == "confirmed":
            return "需求已确认，可回复“继续当前任务”推进内部研究。"
        return f"如认可需求，请回复“确认 ReportBrief v{version}”。"
    return "请先说明报告主题、报告覆盖期和主要阅读对象。"


def _current_record(value: object) -> Mapping[str, object] | None:
    return next((record for record in _mapping_records(value) if record.get("current") is True), None)


def _latest_current_delivery(value: object) -> Mapping[str, object] | None:
    return max(
        (record for record in _mapping_records(value) if record.get("current") is True),
        key=_record_version,
        default=None,
    )


def _append_brief(lines: list[str], value: object) -> None:
    if not isinstance(value, Mapping):
        lines.append("当前任务尚未形成 ReportBrief。")
        return
    lines.append(
        f"ReportBrief v{value.get('contract_version')}：{_status_label(value.get('status'))}。"
    )
    if value.get("status") == "provisional":
        lines.append(
            "如认可，请明确回复"
            f"“确认 ReportBrief v{value.get('contract_version')}”；不要只回复“确认 vN”。"
        )


def _append_gap_decision(lines: list[str], value: object) -> None:
    if not isinstance(value, Mapping):
        return
    lines.extend((
        "最新缺口决策："
        f"v{value.get('contract_version')}，绑定 ReportBrief v{value.get('report_brief_version')}，"
        f"{_status_label(value.get('status'))}，选择={value.get('action')}。",
        "缺口决策绑定版本是历史决策字段，不代表当前 ReportBrief 版本。",
    ))


def _append_outline(lines: list[str], value: object) -> None:
    if not isinstance(value, Mapping):
        lines.append("当前任务尚未形成 ReportOutline。")
        return
    lines.append(
        f"ReportOutline v{value.get('contract_version')}：{_status_label(value.get('status'))}，"
        f"绑定 ReportBrief v{value.get('report_brief_version')}，"
        f"未解决问题 {value.get('unresolved_question_count')} 个。"
    )
    if value.get("status") == "provisional":
        lines.append(
            "如认可，请明确回复"
            f"“确认 ReportOutline v{value.get('contract_version')}”；不要只回复“确认 vN”。"
        )


def _append_draft(lines: list[str], value: object) -> None:
    if not isinstance(value, Mapping):
        lines.append("当前任务尚未形成 ReportDraft。")
        return
    lines.append(
        f"ReportDraft v{value.get('contract_version')}：{_status_label(value.get('status'))}，"
        f"绑定 ReportOutline v{value.get('report_outline_version')}，"
        f"质量检查={value.get('quality_status')}，事实声明 {value.get('claim_count')} 条。"
    )
    if value.get("status") == "provisional":
        lines.append(
            "如认可该初稿，请明确回复"
            f"“确认 ReportDraft v{value.get('contract_version')}”；该确认不等于最终批准。"
        )
    elif value.get("status") == "confirmed":
        lines.append(
            "如需进入内容审批，请另行回复"
            f"“提交 ReportDraft v{value.get('contract_version')} 审批”。"
        )


def _append_approval(lines: list[str], value: object) -> None:
    if not isinstance(value, Mapping):
        lines.append("当前任务尚未形成 ReportApproval。")
        return
    lines.append(
        f"ReportApproval v{value.get('contract_version')}：{_status_label(value.get('status'))}，"
        f"绑定 ReportDraft v{value.get('report_draft_version')}，"
        f"{'当前有效' if value.get('current') is True else '历史记录'}。"
    )
    if value.get("status") == "pending" and value.get("current") is True:
        lines.append(
            "审批人如批准该内容，请明确回复"
            f"“批准 ReportDraft v{value.get('report_draft_version')}”。"
        )
    elif value.get("status") == "approved" and value.get("current") is True:
        lines.append(
            "如需生成文件，请另行回复"
            f"“生成 ReportDraft v{value.get('report_draft_version')} DOCX”。"
        )


def _append_artifacts(lines: list[str], value: object) -> None:
    records = _mapping_records(value)
    if not records:
        lines.append("当前任务尚未形成 ReportArtifact。")
        return
    current = tuple(record for record in records if record.get("current") is True)
    for artifact in current:
        lines.append(
            f"当前文件：ReportArtifact v{artifact.get('report_draft_version')}"
            f"（{str(artifact.get('format') or '').upper()}），"
            f"发布={_status_label(artifact.get('publication_status'))}，"
            f"交付={_status_label(artifact.get('delivery_status'))}。"
        )
    if not current:
        lines.append("当前没有有效的 ReportArtifact。")
    _append_history_count(lines, "ReportArtifact", len(records) - len(current))


def _append_publications(lines: list[str], value: object) -> None:
    records = _mapping_records(value)
    if not records:
        lines.append("当前任务尚未形成 ReportPublication。")
        return
    current = tuple(record for record in records if record.get("current") is True)
    for publication in current:
        lines.append(
            f"当前发布：ReportPublication v{publication.get('publication_version')}，"
            f"绑定 ReportArtifact v{publication.get('report_draft_version')}，"
            f"{_status_label(publication.get('status'))}，"
            f"交付={_status_label(publication.get('delivery_status'))}。"
        )
    if not current:
        lines.append("当前没有有效的 ReportPublication。")
    _append_history_count(lines, "ReportPublication", len(records) - len(current))


def _append_deliveries(lines: list[str], value: object) -> None:
    records = _mapping_records(value)
    if not records:
        lines.append("当前任务尚未形成 ReportDelivery。")
        return
    current = tuple(record for record in records if record.get("current") is True)
    latest = max(current, key=_record_version, default=None)
    if latest is not None:
        lines.append(
            f"最近交付：ReportDelivery v{latest.get('delivery_version')}，"
            f"绑定 ReportArtifact v{latest.get('report_draft_version')}，"
            f"{_status_label(latest.get('status'))}，"
            f"下载授权={_status_label(latest.get('grant_state'))}。"
        )
    else:
        lines.append("当前没有有效的 ReportDelivery。")
    _append_history_count(lines, "ReportDelivery", len(records) - (1 if latest is not None else 0))


def _mapping_records(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        cast("Mapping[str, object]", item)
        for item in value
        if isinstance(item, Mapping)
    )


def _append_if(
    sections: list[ReportStatusSection],
    section: ReportStatusSection,
    condition: bool,
) -> None:
    if condition and section not in sections:
        sections.append(section)


def _append_history_count(lines: list[str], contract_name: str, count: int) -> None:
    if count > 0:
        lines.append(f"历史 {contract_name}：{count} 个，完整记录保留在审计账本中。")


def _record_version(record: Mapping[str, object]) -> int:
    try:
        return int(str(record.get("delivery_version") or "0"))
    except ValueError:
        return 0


def _status_label(value: object) -> str:
    raw = str(value or "未记录").strip()
    return _STATUS_LABELS.get(raw, raw)


def _normalized_command(message: str) -> str:
    return _WHITESPACE_RE.sub("", authenticated_user_command_text(message).strip().lower())
