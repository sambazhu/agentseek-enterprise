from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from agentseek_work import (
    ClaimType,
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
from enterprise_wecom_digital_employee.report_brief import ReportBrief
from enterprise_wecom_digital_employee.report_draft import (
    REPORT_DRAFT_CONTRACT_TYPE,
    DraftClaimProposal,
    DraftQualityStatus,
    ReportDraft,
    build_report_draft,
    prepare_report_draft_context,
)
from enterprise_wecom_digital_employee.report_outline import ReportOutline
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
from enterprise_wecom_digital_employee.work_tools import _build_current_report_outline
from sqlalchemy import create_engine

PROJECT_ROOT = Path(__file__).parents[1]
PACK_ROOT = PROJECT_ROOT / "digital_employees" / "industry-report"
ASSET_REF = "trusted-asset://strategic-report-docx/1.0.0"
NOW = datetime(2026, 7, 18, 9, 0, tzinfo=UTC)
CONTENT = "证券行业数字化转型应以客户服务、经营管理和风险控制能力提升为目标。"


def test_prepare_evidence_build_draft_and_save_idempotently(tmp_path: Path) -> None:
    composition, state, outline = _composition_with_confirmed_outline(tmp_path)
    calls: list[tuple[str, str, dict[str, Any], bool]] = []

    async def invoke(server: str, tool_name: str, arguments: dict[str, Any], confirmed: bool) -> str:
        calls.append((server, tool_name, arguments, confirmed))
        return json.dumps({
            "chunks": [{"chunk_id": "chunk-1", "content": CONTENT}],
        }, ensure_ascii=False)

    context = _run(prepare_report_draft_context(
        composition=composition,
        state=state,
        runtime_context=None,
        invoke_mcp=invoke,
        clock=lambda: NOW,
    ))
    replay = _run(prepare_report_draft_context(
        composition=composition,
        state=state,
        runtime_context=None,
        invoke_mcp=invoke,
        clock=lambda: NOW,
    ))

    assert len(context.evidence) == 1
    assert replay.evidence == context.evidence
    assert calls[0] == (
        "department-knowledge",
        "knowledge_read_chunks",
        {"chunk_ids": ["chunk-1"]},
        False,
    )
    evidence_id = context.evidence[0].evidence_id
    proposals = _proposals(outline, evidence_id)
    draft = build_report_draft(
        composition=composition,
        state=state,
        runtime_context=None,
        proposals=proposals,
        clock=lambda: NOW,
    )
    first = composition.save_report_draft(state, None, draft)
    replay_contract = composition.save_report_draft(state, None, draft)

    assert first.contract_type == REPORT_DRAFT_CONTRACT_TYPE
    assert first.contract_version == replay_contract.contract_version == 1
    assert first.status is WorkContractStatus.PROVISIONAL
    assert ReportDraft.from_contract(first) == draft
    assert draft.quality_status is DraftQualityStatus.WARNING
    assert "[E1]" in draft.markdown
    assert "待确认问题" in draft.markdown
    assert len(composition.repository.list_evidence_records(
        tenant_id="tenant-test",
        work_id="work_draft_001",
    )) == 1
    claims = composition.repository.list_claim_records(
        tenant_id="tenant-test",
        work_id="work_draft_001",
    )
    assert len(claims) == len(outline.sections)
    assert all(claim.verification_status.value == "unverified" for claim in claims)
    summary = composition.current_work_summary(state)
    assert summary is not None
    assert summary["report_draft"] == {
        "contract_version": 1,
        "status": "provisional",
        "report_outline_version": 1,
        "quality_status": "warning",
        "claim_count": len(outline.sections),
    }


def test_draft_requires_confirmed_outline_and_source_hash_stability(tmp_path: Path) -> None:
    composition, state = _confirmed_brief_composition(tmp_path)

    with pytest.raises(WorkCompositionError, match="已确认的 ReportOutline"):
        build_report_draft(
            composition=composition,
            state=state,
            runtime_context=None,
            proposals=[DraftClaimProposal(
                section_id="executive-summary",
                statement="待确认。",
                claim_type=ClaimType.RISK,
            )],
        )

    composition, state, _outline = _composition_with_confirmed_outline(tmp_path / "confirmed")

    async def changed(_server: str, _tool: str, _arguments: dict[str, Any], _confirmed: bool) -> str:
        return json.dumps({"chunks": [{"chunk_id": "chunk-1", "content": "内容已变化"}]})

    with pytest.raises(RuntimeError, match="content changed"):
        _run(prepare_report_draft_context(
            composition=composition,
            state=state,
            runtime_context=None,
            invoke_mcp=changed,
        ))


def test_draft_rejects_unsupported_fact_and_sensitive_content(tmp_path: Path) -> None:
    composition, state, outline = _composition_with_confirmed_outline(tmp_path)

    with pytest.raises(ValueError, match="必须绑定 EvidenceRecord"):
        build_report_draft(
            composition=composition,
            state=state,
            runtime_context=None,
            proposals=[
                DraftClaimProposal(
                    section_id=section.section_id,
                    statement="这是没有证据的事实。",
                    claim_type=ClaimType.FACT,
                )
                for section in outline.sections
            ],
        )
    with pytest.raises(ValueError, match="凭据样式"):
        build_report_draft(
            composition=composition,
            state=state,
            runtime_context=None,
            proposals=[
                DraftClaimProposal(
                    section_id=section.section_id,
                    statement="password=top-secret" if index == 0 else "本章节待补充。",
                    claim_type=ClaimType.RISK,
                )
                for index, section in enumerate(outline.sections)
            ],
        )


def test_draft_validates_the_complete_claim_batch_before_writing(tmp_path: Path) -> None:
    composition, state, outline = _composition_with_confirmed_outline(tmp_path)
    proposals = [
        DraftClaimProposal(
            section_id=section.section_id,
            statement=("这是可保存的风险提示。" if index == 0 else "这是没有证据的事实。"),
            claim_type=ClaimType.RISK if index == 0 else ClaimType.FACT,
        )
        for index, section in enumerate(outline.sections)
    ]

    with pytest.raises(ValueError, match="必须绑定 EvidenceRecord"):
        build_report_draft(
            composition=composition,
            state=state,
            runtime_context=None,
            proposals=proposals,
        )

    assert composition.repository.list_claim_records(
        tenant_id="tenant-test",
        work_id="work_draft_001",
    ) == ()


def _composition_with_confirmed_outline(
    tmp_path: Path,
) -> tuple[IndustryReportWorkComposition, dict[str, Any], ReportOutline]:
    composition, state = _confirmed_brief_composition(tmp_path)
    internal = load_current_research_result(
        composition=composition,
        state=state,
        runtime_context=None,
        template_path=composition.research_template_path,
    )
    composition.repository.put_source_record(SourceRecord(
        source_id="source_draft_001",
        work_id="work_draft_001",
        tenant_id="tenant-test",
        source_type=SourceType.DEPARTMENT_KNOWLEDGE,
        title="证券行业数字化转型规划",
        publisher="战略发展部",
        retrieved_at=NOW,
        locator="mcp://department-knowledge/doc-1#chunk-1",
        uri_digest="sha256:uri",
        content_hash=_digest_text(CONTENT),
        result_digest="sha256:result",
        confidentiality_level="internal",
        authority_level="approved_internal",
        allowed_uses=("research", "citation"),
        snapshot_policy="reference_only",
        snapshot_status=SnapshotStatus.REFERENCED,
        retrieval_query_digest="sha256:query",
        excerpt_status=ExcerptStatus.NOT_REQUESTED,
        license_terms_ref="internal-policy://department-knowledge/v1",
        metadata={
            "provider": "department-knowledge",
            "document_id": "doc-1",
            "chunk_id": "chunk-1",
            "section_ids": ["executive-summary"],
            "question_ids": ["executive-summary.core-trends"],
            "research_plan_digest": internal.plan.digest,
            "report_brief_version": 1,
        },
    ))
    refreshed = load_current_research_result(
        composition=composition,
        state=state,
        runtime_context=None,
        template_path=composition.research_template_path,
    )
    authorization = "ReportBrief v1 保留缺口继续生成"
    composition.confirm_research_gap_decision(
        state,
        None,
        decision=ResearchGapDecision(
            report_brief_version=1,
            research_plan_digest=refreshed.plan.digest,
            gap_digest=gap_digest(refreshed.coverage.gaps),
            gap_question_ids=refreshed.coverage.gaps,
            action=ResearchGapAction.CONTINUE_WITH_GAPS,
            authorization_message_digest=message_digest(authorization),
        ),
        latest_user_message=authorization,
    )
    outline = _build_current_report_outline(composition, state, None)
    contract = composition.save_report_outline(state, None, outline)
    composition.confirm_report_outline(
        state,
        None,
        expected_version=contract.contract_version,
        latest_user_message="确认 ReportOutline v1。",
    )
    return composition, state, outline


def _confirmed_brief_composition(
    tmp_path: Path,
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
        id_factory=lambda: "work_draft_001",
    )
    state = _authorized_state()
    request = {
        "content": "请创建证券行业数字化转型报告",
        "context": {"wecom": {"raw": {"msgid": "message-draft-001"}}},
    }
    composition.enrich_state(request, "wecom:test", state)
    composition.create_report_work(state)
    brief = composition.save_report_brief(
        state,
        None,
        ReportBrief(
            title="证券行业数字化转型报告",
            target_audience=("公司管理层",),
            coverage_period="2026年全年",
        ),
    )
    composition.confirm_report_brief(
        state,
        None,
        expected_version=brief.contract_version,
        latest_user_message="确认 ReportBrief v1。",
    )
    return composition, state


def _proposals(outline: ReportOutline, evidence_id: str) -> list[DraftClaimProposal]:
    proposals: list[DraftClaimProposal] = []
    for index, section in enumerate(outline.sections):
        proposals.append(DraftClaimProposal(
            section_id=section.section_id,
            statement=(
                "证券行业数字化转型应同时提升客户服务、经营管理和风险控制能力。"
                if index == 0
                else f"{section.title}仍有研究问题需要后续确认。"
            ),
            claim_type=ClaimType.FACT if index == 0 else ClaimType.RISK,
            evidence_ids=[evidence_id] if index == 0 else [],
        ))
    return proposals


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


def _digest_text(value: str) -> str:
    return f"sha256:{sha256(value.encode()).hexdigest()}"


def _run(awaitable):
    import asyncio

    return asyncio.run(awaitable)
