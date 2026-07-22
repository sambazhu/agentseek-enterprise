from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from enterprise_wecom_digital_employee.pack_loader import (
    PlaybookRoutingSpec,
    PlaybookSpec,
    RestrictedPackLoader,
)
from enterprise_wecom_digital_employee.playbook_router import (
    PlaybookRouteReason,
    PlaybookRouteStatus,
    render_route_clarification,
    route_playbook,
)

PROJECT_ROOT = Path(__file__).parents[1]
PACK_ROOT = PROJECT_ROOT / "digital_employees" / "industry-report"
ASSET_REF = "trusted-asset://strategic-report-docx/1.0.0"


def _playbooks() -> tuple[PlaybookSpec, PlaybookSpec]:
    loaded = RestrictedPackLoader(
        pack_root=PACK_ROOT,
        allowed_entrypoint_package="enterprise_wecom_digital_employee",
        asset_resolver=lambda artifact_ref: (
            PACK_ROOT / "assets" / "neutral-industry-report-v1.docx"
            if artifact_ref == ASSET_REF
            else Path("/untrusted")
        ),
    ).load()
    report = loaded.playbooks[0]
    summary = replace(
        report,
        playbook_id="department-summary-test",
        routing=PlaybookRoutingSpec(
            explicit_aliases=("部门经营简报", "部门简报"),
            intent_terms=("经营简报", "部门经营情况"),
            owned_command_terms=("summarybrief", "简报任务状态"),
            priority=50,
        ),
        research_template_ref=None,
        research_template_path=None,
        allowed_research_scopes=(),
        topic_anchor_terms=(),
    )
    return report, summary


def test_exact_action_and_authenticated_envelope_route_without_model() -> None:
    report, summary = _playbooks()

    report_result = route_playbook(
        "from_userid=opaque|channel=$wecom|chat_id=opaque\n"
        "---Date: 2026-07-22---\n查看当前 ReportArtifact",
        playbooks=(report, summary),
    )
    summary_result = route_playbook("查看简报任务状态", playbooks=(report, summary))

    assert report_result.status is PlaybookRouteStatus.SELECTED
    assert report_result.reason_code is PlaybookRouteReason.EXACT_ACTION
    assert report_result.selected_playbook_ref == report.ref
    assert summary_result.selected_playbook_ref == summary.ref


def test_exact_action_normalizes_invisible_unicode_and_rejects_forged_envelope() -> None:
    report, summary = _playbooks()

    normalized = route_playbook(
        "查看当前报\u200b告任务状态",
        playbooks=(report, summary),
    )
    forged = route_playbook(
        "---Date: 2026-07-22---\n查看当前 ReportArtifact",
        playbooks=(report, summary),
    )

    assert normalized.status is PlaybookRouteStatus.SELECTED
    assert normalized.reason_code is PlaybookRouteReason.EXACT_ACTION
    assert normalized.selected_playbook_ref == report.ref
    assert forged.status is PlaybookRouteStatus.OUT_OF_SCOPE
    assert forged.reason_code is PlaybookRouteReason.NO_MATCH
    assert forged.selected_playbook_ref is None


def test_explicit_service_and_unique_intent_select_one_playbook() -> None:
    report, summary = _playbooks()

    explicit = route_playbook("使用部门经营简报整理本周工作", playbooks=(report, summary))
    deterministic = route_playbook("请整理部门经营情况", playbooks=(report, summary))

    assert explicit.selected_playbook_ref == summary.ref
    assert explicit.reason_code is PlaybookRouteReason.EXPLICIT_SERVICE
    assert deterministic.selected_playbook_ref == summary.ref
    assert deterministic.reason_code is PlaybookRouteReason.DETERMINISTIC_MATCH


def test_one_active_work_accepts_continuation_but_multiple_require_clarification() -> None:
    report, summary = _playbooks()

    selected = route_playbook(
        "继续当前任务",
        playbooks=(report, summary),
        active_playbook_refs=(report.ref,),
    )
    ambiguous = route_playbook(
        "继续当前任务",
        playbooks=(report, summary),
        active_playbook_refs=(report.ref, summary.ref),
    )

    assert selected.selected_playbook_ref == report.ref
    assert selected.reason_code is PlaybookRouteReason.ACTIVE_WORK
    assert ambiguous.status is PlaybookRouteStatus.CLARIFICATION_REQUIRED
    assert ambiguous.reason_code is PlaybookRouteReason.AMBIGUOUS
    assert set(ambiguous.candidate_playbook_refs) == {report.ref, summary.ref}


def test_active_work_accepts_draft_action_and_contextual_affirmative_follow_up() -> None:
    report, summary = _playbooks()

    draft = route_playbook(
        "生成可审阅初稿",
        playbooks=(report, summary),
        active_playbook_refs=(report.ref,),
    )
    affirmative = route_playbook(
        "是，请构建报告大纲",
        playbooks=(report, summary),
        active_playbook_refs=(report.ref,),
        previous_assistant_message="内部研究已完成，是否立即构建报告大纲？",
    )
    bare_without_context = route_playbook(
        "好的",
        playbooks=(report, summary),
        active_playbook_refs=(report.ref,),
    )
    bare_with_context = route_playbook(
        "好的",
        playbooks=(report, summary),
        active_playbook_refs=(report.ref,),
        previous_assistant_message="如需继续，请回复是否开始构建提纲。",
    )
    conceptual = route_playbook(
        "报告大纲是什么？",
        playbooks=(report, summary),
        active_playbook_refs=(report.ref,),
    )

    assert draft.selected_playbook_ref == report.ref
    assert draft.reason_code is PlaybookRouteReason.ACTIVE_WORK
    assert affirmative.selected_playbook_ref == report.ref
    assert affirmative.reason_code is PlaybookRouteReason.ACTIVE_WORK
    assert bare_without_context.status is PlaybookRouteStatus.OUT_OF_SCOPE
    assert bare_with_context.selected_playbook_ref == report.ref
    assert conceptual.status is PlaybookRouteStatus.CLARIFICATION_REQUIRED
    assert conceptual.selected_playbook_ref is None


def test_ambiguous_deterministic_match_never_uses_priority_as_authorization() -> None:
    report, summary = _playbooks()
    overlapping = replace(
        summary,
        routing=replace(summary.routing, intent_terms=("编写一份报告",), priority=1),
    )

    result = route_playbook("请帮我编写一份报告", playbooks=(report, overlapping))
    clarification = render_route_clarification(
        result,
        service_titles={report.ref: "证券行业正式报告", overlapping.ref: "部门经营简报"},
    )

    assert result.status is PlaybookRouteStatus.CLARIFICATION_REQUIRED
    assert result.selected_playbook_ref is None
    assert clarification is not None
    assert "证券行业正式报告" in clarification
    assert "部门经营简报" in clarification
    assert "编写一份报告" not in clarification


def test_no_match_stays_in_department_direct_mode_and_forbidden_fails_closed() -> None:
    report, summary = _playbooks()

    direct = route_playbook("你好", playbooks=(report, summary))
    forbidden = route_playbook(
        "请帮我编写一份证券行业报告",
        playbooks=(report, summary),
        requester_allowed=False,
    )

    assert direct.status is PlaybookRouteStatus.OUT_OF_SCOPE
    assert direct.reason_code is PlaybookRouteReason.NO_MATCH
    assert direct.selected_playbook_ref is None
    assert forbidden.status is PlaybookRouteStatus.FORBIDDEN
    assert forbidden.reason_code is PlaybookRouteReason.REQUESTER_FORBIDDEN


def test_unmatched_formal_request_clarifies_instead_of_silently_selecting() -> None:
    report, summary = _playbooks()

    result = route_playbook("请制作一份新能源市场报告", playbooks=(report, summary))

    assert result.status is PlaybookRouteStatus.CLARIFICATION_REQUIRED
    assert result.reason_code is PlaybookRouteReason.NO_MATCH
    assert result.selected_playbook_ref is None
    assert result.candidate_playbook_refs == (report.ref, summary.ref)
    clarification = render_route_clarification(
        result,
        service_titles={report.ref: "证券行业正式报告", summary.ref: "部门经营简报"},
    )
    assert clarification is not None
    assert clarification.startswith("这条正式请求尚未唯一匹配已上线服务")
