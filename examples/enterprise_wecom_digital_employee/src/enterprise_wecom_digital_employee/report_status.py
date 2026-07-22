from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import cast

from enterprise_wecom_digital_employee.channel_command import authenticated_user_command_text


class ReportStatusSection(StrEnum):
    BRIEF = "report_brief"
    GAP_DECISION = "research_gap_decision"
    OUTLINE = "report_outline"
    DRAFT = "report_draft"
    APPROVAL = "report_approval"
    ARTIFACT = "report_artifacts"
    PUBLICATION = "report_publications"
    DELIVERY = "report_deliveries"


_ALL_SECTIONS = tuple(ReportStatusSection)
_READ_PREFIX_RE = re.compile(r"^(?:请)?(?:查看|查询|显示|告诉我)(?:一下)?")
_WHITESPACE_RE = re.compile(r"\s+")


def match_report_status_sections(message: str) -> tuple[ReportStatusSection, ...] | None:
    """Recognize a bounded read-only report-ledger query without a model."""

    command = _normalized_command(message)
    if not _READ_PREFIX_RE.match(command) or "当前" not in command:
        return None
    if "报告任务状态" in command or "当前任务状态" in command or "当前报告状态" in command:
        return _ALL_SECTIONS

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


def render_report_status(
    summary: Mapping[str, object] | None,
    *,
    sections: Sequence[ReportStatusSection] = _ALL_SECTIONS,
) -> str:
    """Render ledger-backed status text without model-authored business claims."""

    if summary is None:
        return "当前员工没有可见的进行中任务。"
    selected = frozenset(sections)
    lines = [
        f"当前任务：work_id={summary['work_id']}，status={summary['status']}，"
        f"phase={summary['current_phase']}，"
        f"playbook={summary['playbook_id']}@{summary['playbook_version']}。"
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
    return "\n".join(lines)


def _append_brief(lines: list[str], value: object) -> None:
    if not isinstance(value, Mapping):
        lines.append("当前任务尚未形成 ReportBrief。")
        return
    lines.append(f"当前 ReportBrief：v{value.get('contract_version')}，status={value.get('status')}。")
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
        f"contract_v{value.get('contract_version')}，"
        f"bound_report_brief_v{value.get('report_brief_version')}，"
        f"status={value.get('status')}，action={value.get('action')}。",
        "缺口决策绑定版本是历史决策字段，不代表当前 ReportBrief 版本。",
    ))


def _append_outline(lines: list[str], value: object) -> None:
    if not isinstance(value, Mapping):
        lines.append("当前任务尚未形成 ReportOutline。")
        return
    lines.append(
        "当前 ReportOutline："
        f"v{value.get('contract_version')}，status={value.get('status')}，"
        f"bound_report_brief_v{value.get('report_brief_version')}，"
        f"unresolved={value.get('unresolved_question_count')}。"
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
        "当前 ReportDraft："
        f"v{value.get('contract_version')}，status={value.get('status')}，"
        f"bound_report_outline_v{value.get('report_outline_version')}，"
        f"quality={value.get('quality_status')}，claims={value.get('claim_count')}。"
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
        "当前 ReportApproval："
        f"contract_v{value.get('contract_version')}，status={value.get('status')}，"
        f"bound_report_draft_v{value.get('report_draft_version')}，"
        f"current={str(bool(value.get('current'))).lower()}。"
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
    for artifact in records:
        lines.append(
            "当前 ReportArtifact："
            f"artifact_id={artifact.get('artifact_id')}，format={artifact.get('format')}，"
            f"bound_report_draft_v{artifact.get('report_draft_version')}，"
            f"current={str(bool(artifact.get('current'))).lower()}，"
            f"publication={artifact.get('publication_status')}，"
            f"delivery={artifact.get('delivery_status')}。"
        )


def _append_publications(lines: list[str], value: object) -> None:
    records = _mapping_records(value)
    if not records:
        lines.append("当前任务尚未形成 ReportPublication。")
        return
    for publication in records:
        lines.append(
            "当前 ReportPublication："
            f"publication_v{publication.get('publication_version')}，"
            f"status={publication.get('status')}，"
            f"artifact_id={publication.get('artifact_id')}，"
            f"bound_report_draft_v{publication.get('report_draft_version')}，"
            f"current={str(bool(publication.get('current'))).lower()}，"
            f"delivery={publication.get('delivery_status')}。"
        )


def _append_deliveries(lines: list[str], value: object) -> None:
    records = _mapping_records(value)
    if not records:
        lines.append("当前任务尚未形成 ReportDelivery。")
        return
    for delivery in records:
        lines.append(
            "当前 ReportDelivery："
            f"delivery_v{delivery.get('delivery_version')}，"
            f"status={delivery.get('status')}，"
            f"artifact_id={delivery.get('artifact_id')}，"
            f"bound_report_draft_v{delivery.get('report_draft_version')}，"
            f"current={str(bool(delivery.get('current'))).lower()}，"
            f"grant_state={delivery.get('grant_state')}。"
        )


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


def _normalized_command(message: str) -> str:
    return _WHITESPACE_RE.sub("", authenticated_user_command_text(message).strip().lower())
