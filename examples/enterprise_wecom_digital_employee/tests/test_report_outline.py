from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from agentseek_work import (
    ExcerptStatus,
    SnapshotStatus,
    SourceRecord,
    SourceType,
    SQLAlchemyWorkRepository,
    WorkContractStatus,
    apply_migrations,
)
from enterprise_wecom_digital_employee.pack_loader import (
    FilesystemPackSnapshotStore,
    RestrictedPackLoader,
    build_pack_snapshot,
)
from enterprise_wecom_digital_employee.report_brief import ReportBrief, ResearchScope
from enterprise_wecom_digital_employee.report_outline import (
    REPORT_OUTLINE_CONTRACT_TYPE,
    OutlineEvidenceStatus,
    OutlineQuestion,
    OutlineSection,
    ReportOutline,
    explicitly_confirms_report_outline,
    source_set_digest,
)
from enterprise_wecom_digital_employee.report_research import load_current_research_result
from enterprise_wecom_digital_employee.research_gap_decision import (
    ResearchGapAction,
    ResearchGapDecision,
    gap_digest,
    message_digest,
)
from enterprise_wecom_digital_employee.work_composition import (
    IndustryReportWorkComposition,
    WorkCompositionError,
)
from enterprise_wecom_digital_employee.work_tools import (
    _build_current_report_outline,
    _format_outline,
)
from sqlalchemy import create_engine

PROJECT_ROOT = Path(__file__).parents[1]
PACK_ROOT = PROJECT_ROOT / "digital_employees" / "industry-report"
ASSET_REF = "trusted-asset://strategic-report-docx/1.0.0"
NOW = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)


def test_report_outline_round_trips_and_derives_statuses() -> None:
    outline = ReportOutline(
        report_brief_version=3,
        research_plan_digest="sha256:plan",
        research_scope="securities_industry",
        report_title="证券行业数字化转型报告",
        template_id="securities-industry-internal-research",
        template_version="2.0.0",
        source_set_digest="sha256:sources",
        gap_decision_contract_version=2,
        sections=(
            OutlineSection(
                section_id="executive-summary",
                title="执行摘要",
                questions=(
                    OutlineQuestion("executive-summary.topic", "主题直接证据？", ("source_1",)),
                    OutlineQuestion("executive-summary.trends", "核心趋势？"),
                ),
            ),
        ),
    )
    contract = outline.to_contract(
        work_id="work_001",
        tenant_id="tenant_001",
        contract_version=1,
        created_by="employee_001",
        created_at=NOW,
    )

    assert contract.contract_type == REPORT_OUTLINE_CONTRACT_TYPE
    assert contract.status is WorkContractStatus.PROVISIONAL
    assert contract.payload["sections"][0]["status"] == "partial"
    assert contract.payload["unresolved_question_ids"] == ["executive-summary.trends"]
    assert ReportOutline.from_contract(contract) == outline


@pytest.mark.parametrize(
    "message",
    [
        "确认 ReportOutline v2。",
        "我同意 Report Outline version 2。",
        "批准报告提纲第2版。",
    ],
)
def test_report_outline_confirmation_requires_explicit_exact_version(message: str) -> None:
    assert explicitly_confirms_report_outline(message, expected_version=2)


@pytest.mark.parametrize(
    "message",
    [
        "确认",
        "确认 v2",
        "确认 ReportOutline v1",
        "不要确认 ReportOutline v2",
        "请确认 ReportOutline v2",
        "是否确认 ReportOutline v2？",
        "确认 ReportOutline v2 和 ReportOutline v3",
    ],
)
def test_report_outline_confirmation_rejects_implicit_or_ambiguous_messages(message: str) -> None:
    assert not explicitly_confirms_report_outline(message, expected_version=2)


def test_outline_requires_current_gap_decision_then_saves_confirms_and_replays(tmp_path: Path) -> None:
    composition, state = _confirmed_composition(tmp_path)

    with pytest.raises(WorkCompositionError, match="先对当前 ReportBrief"):
        _build_current_report_outline(composition, state, None)

    _confirm_continue_with_gaps(composition, state)
    outline = _build_current_report_outline(composition, state, None)
    first = composition.save_report_outline(state, None, outline)
    replay = composition.save_report_outline(state, None, outline)

    assert first.contract_version == replay.contract_version == 1
    assert first.status is WorkContractStatus.PROVISIONAL
    assert len(outline.sections) == 5
    assert len(outline.unresolved_question_ids) == 6
    assert all(
        question.evidence_status is OutlineEvidenceStatus.UNRESOLVED
        for section in outline.sections
        for question in section.questions
    )
    with pytest.raises(WorkCompositionError, match="未显式确认"):
        composition.confirm_report_outline(
            state,
            None,
            expected_version=1,
            latest_user_message="请开始写报告。",
        )
    confirmed = composition.confirm_report_outline(
        state,
        None,
        expected_version=1,
        latest_user_message="确认 ReportOutline v1。",
    )
    summary = composition.current_work_summary(state)

    assert confirmed.status is WorkContractStatus.CONFIRMED
    assert summary is not None
    assert summary["report_outline"] == {
        "contract_version": 1,
        "status": "confirmed",
        "report_brief_version": 1,
        "source_set_digest": source_set_digest(()),
        "unresolved_question_count": 6,
    }
    formatted = _format_outline(1, "confirmed", outline)
    assert "不含报告正文" in formatted
    assert len(formatted) < 1200


def test_outline_waits_for_uploaded_materials(tmp_path: Path) -> None:
    composition, state = _confirmed_composition(tmp_path)
    _confirm_gap_action(composition, state, ResearchGapAction.UPLOAD_MATERIALS)

    with pytest.raises(WorkCompositionError, match="等待员工上传材料"):
        _build_current_report_outline(composition, state, None)


def test_external_factor_outline_omits_not_applicable_sections(tmp_path: Path) -> None:
    composition, state = _confirmed_composition(
        tmp_path,
        title="新能源汽车变化对证券行业的影响",
        research_scope=ResearchScope.EXTERNAL_FACTOR_ON_SECURITIES,
    )
    _confirm_continue_with_gaps(composition, state)

    outline = _build_current_report_outline(composition, state, None)

    assert tuple(section.section_id for section in outline.sections) == (
        "executive-summary",
        "action-recommendations",
    )
    assert sum(len(section.questions) for section in outline.sections) == 3


def test_outline_confirmation_fails_when_source_set_changes(tmp_path: Path) -> None:
    composition, state = _confirmed_composition(tmp_path)
    decision = _confirm_continue_with_gaps(composition, state)
    outline = _build_current_report_outline(composition, state, None)
    contract = composition.save_report_outline(state, None, outline)
    internal = load_current_research_result(
        composition=composition,
        state=state,
        runtime_context=None,
        template_path=composition.research_template_path,
    )
    source = _source_record(
        plan_digest=internal.plan.digest,
        question_id="executive-summary.core-trends",
        gap_contract_version=decision.contract_version,
    )
    composition.repository.put_source_record(source)

    with pytest.raises(WorkCompositionError, match="来源集合已变化"):
        composition.confirm_report_outline(
            state,
            None,
            expected_version=contract.contract_version,
            latest_user_message="确认 ReportOutline v1。",
        )


def test_outline_parser_rejects_inconsistent_derived_fields() -> None:
    question = OutlineQuestion("q1", "问题", ("source_1",))
    outline = ReportOutline(
        report_brief_version=1,
        research_plan_digest="sha256:plan",
        research_scope="securities_industry",
        report_title="证券行业报告",
        template_id="template",
        template_version="1",
        source_set_digest="sha256:sources",
        sections=(OutlineSection("s1", "章节", (question,)),),
    )
    contract = outline.to_contract(
        work_id="work_001",
        tenant_id="tenant_001",
        contract_version=1,
        created_by="employee_001",
        created_at=NOW,
    )
    corrupt = replace(contract, payload={**contract.payload, "source_ids": []})

    with pytest.raises(ValueError, match="source_ids are inconsistent"):
        ReportOutline.from_contract(corrupt)


def _confirmed_composition(
    tmp_path: Path,
    *,
    title: str = "证券行业数字化转型报告",
    research_scope: ResearchScope = ResearchScope.SECURITIES_INDUSTRY,
) -> tuple[IndustryReportWorkComposition, dict[str, Any]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    apply_migrations(engine)
    repository = SQLAlchemyWorkRepository(engine)

    def resolve_asset(artifact_ref: str) -> Path:
        assert artifact_ref == ASSET_REF
        return PACK_ROOT / "assets" / "neutral-industry-report-v1.docx"

    loaded = RestrictedPackLoader(
        pack_root=PACK_ROOT,
        allowed_entrypoint_package="enterprise_wecom_digital_employee",
        asset_resolver=resolve_asset,
    ).load()
    snapshot_store = FilesystemPackSnapshotStore(tmp_path / "snapshots")
    snapshot = build_pack_snapshot(loaded, store=snapshot_store, created_at=NOW)
    repository.put_pack_snapshot(snapshot)
    composition = IndustryReportWorkComposition(
        repository=repository,
        loaded_pack=loaded,
        pack_snapshot_id=snapshot.pack_snapshot_id,
        runtime_release="enterprise-wecom-v0.1.0-m3",
        pack_artifact_root=snapshot_store.resolve(snapshot.content_artifact_id),
        clock=lambda: NOW,
        id_factory=lambda: "work_outline_001",
    )
    state = _authorized_state()
    request = {
        "content": "请创建证券行业报告",
        "context": {"wecom": {"raw": {"msgid": "message-outline-001"}}},
    }
    composition.enrich_state(request, "wecom:test", state)
    composition.create_report_work(state)
    brief = composition.save_report_brief(
        state,
        None,
        ReportBrief(
            title=title,
            research_scope=research_scope,
            target_audience=("公司管理层",),
            coverage_period="2026年全年",
        ),
    )
    composition.confirm_report_brief(
        state,
        None,
        expected_version=brief.contract_version,
        latest_user_message=f"确认 ReportBrief v{brief.contract_version}。",
    )
    return composition, state


def _confirm_continue_with_gaps(
    composition: IndustryReportWorkComposition,
    state: dict[str, Any],
):
    return _confirm_gap_action(composition, state, ResearchGapAction.CONTINUE_WITH_GAPS)


def _confirm_gap_action(
    composition: IndustryReportWorkComposition,
    state: dict[str, Any],
    action: ResearchGapAction,
):
    internal = load_current_research_result(
        composition=composition,
        state=state,
        runtime_context=None,
        template_path=composition.research_template_path,
    )
    text = {
        ResearchGapAction.CONTINUE_WITH_GAPS: "ReportBrief v1 保留缺口继续生成",
        ResearchGapAction.UPLOAD_MATERIALS: "为 ReportBrief v1 上传补充材料",
    }[action]
    decision = ResearchGapDecision(
        report_brief_version=internal.plan.report_brief_version,
        research_plan_digest=internal.plan.digest,
        gap_digest=gap_digest(internal.coverage.gaps),
        gap_question_ids=internal.coverage.gaps,
        action=action,
        authorization_message_digest=message_digest(text),
    )
    return composition.confirm_research_gap_decision(
        state,
        None,
        decision=decision,
        latest_user_message=text,
    )


def _source_record(
    *,
    plan_digest: str,
    question_id: str,
    gap_contract_version: int,
) -> SourceRecord:
    return SourceRecord(
        source_id="source_outline_change",
        work_id="work_outline_001",
        tenant_id="tenant-test",
        source_type=SourceType.PUBLIC_WEB,
        title="新增公开来源",
        publisher="测试",
        retrieved_at=NOW,
        locator="https://example.test/source",
        uri_digest="sha256:uri",
        content_hash="sha256:content",
        result_digest="sha256:result",
        confidentiality_level="internal",
        authority_level="public_web",
        allowed_uses=("research", "citation"),
        snapshot_policy="reference_only",
        snapshot_status=SnapshotStatus.REFERENCED,
        retrieval_query_digest="sha256:query",
        excerpt_status=ExcerptStatus.NOT_REQUESTED,
        metadata={
            "question_ids": [question_id],
            "section_ids": ["executive-summary"],
            "report_brief_version": 1,
            "research_plan_digest": plan_digest,
            "gap_decision_contract_version": gap_contract_version,
        },
    )


def _authorized_state() -> dict[str, Any]:
    return {
        "employee_context": {
            "name": "测试员工",
            "oa_account": "not-published",
            "dept_name": "战略发展部",
            "org_path_label": "总部/战略发展部",
        },
        "_langgraph_runtime_context": {
            "enterprise": {
                "version": "v1",
                "tenant_id": "tenant-test",
                "tenant_key": f"hmac-{'1' * 64}",
                "user_key": f"hmac-{'2' * 64}",
                "session_key": f"hmac-{'3' * 64}",
            }
        },
    }
