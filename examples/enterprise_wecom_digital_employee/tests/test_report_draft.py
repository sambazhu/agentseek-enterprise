from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import pytest
from agentseek_wecom.outbound import ArtifactDownloadGone
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
from enterprise_wecom_digital_employee.report_approval import (
    REPORT_APPROVAL_CONTRACT_TYPE,
    ReportApproval,
    approval_state,
    explicitly_approves_report_draft,
    explicitly_requests_report_approval,
)
from enterprise_wecom_digital_employee.report_brief import ReportBrief
from enterprise_wecom_digital_employee.report_draft import (
    REPORT_DRAFT_CONTRACT_TYPE,
    DraftClaimProposal,
    DraftQualityStatus,
    ReportDraft,
    build_report_draft,
    explicitly_confirms_report_draft,
    explicitly_requests_report_draft,
    prepare_report_draft_context,
    report_draft_digest,
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
DRAFT_REQUEST = "请生成可审阅初稿"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("请生成可审阅初稿", True),
        ("请根据已生成的提纲生成初稿", True),
        ("开始撰写报告草稿", True),
        ("generate a report draft", True),
        ("确认 ReportOutline v1", False),
        ("confirm ReportDraft v1", False),
        ("暂不生成初稿", False),
        ("可以生成初稿吗？", False),
        ("ReportDraft v1 已生成", False),
        ("初稿状态如何", False),
    ],
)
def test_explicit_draft_request_parser(message: str, expected: bool) -> None:
    assert explicitly_requests_report_draft(message) is expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("确认 ReportDraft v2", True),
        ("我认可报告初稿第2版", True),
        ("确认", False),
        ("确认 ReportDraft v1", False),
        ("不确认 ReportDraft v2", False),
        ("请确认 ReportDraft v2", False),
        ("ReportDraft v2 可以确认吗？", False),
    ],
)
def test_explicit_draft_confirmation_parser(message: str, expected: bool) -> None:
    assert explicitly_confirms_report_draft(message, expected_version=2) is expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("提交 ReportDraft v2 审批", True),
        ("申请报告初稿第2版审批", True),
        ("send ReportDraft v2 for approval", False),
        ("确认 ReportDraft v2", False),
        ("提交 ReportDraft v1 审批", False),
        ("暂不提交 ReportDraft v2 审批", False),
        ("可以提交 ReportDraft v2 审批吗？", False),
    ],
)
def test_explicit_approval_request_parser(message: str, expected: bool) -> None:
    assert explicitly_requests_report_approval(message, expected_version=2) is expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("批准 ReportDraft v2", True),
        ("报告初稿第2版审批通过", True),
        ("approve ReportDraft v2", True),
        ("提交 ReportDraft v2 审批", False),
        ("请批准 ReportDraft v2", False),
        ("批准 ReportDraft v1", False),
        ("不批准 ReportDraft v2", False),
        ("ReportDraft v2 是否批准？", False),
    ],
)
def test_explicit_draft_approval_parser(message: str, expected: bool) -> None:
    assert explicitly_approves_report_draft(message, expected_version=2) is expected


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
        latest_user_message=DRAFT_REQUEST,
        invoke_mcp=invoke,
        clock=lambda: NOW,
    ))
    replay = _run(prepare_report_draft_context(
        composition=composition,
        state=state,
        runtime_context=None,
        latest_user_message=DRAFT_REQUEST,
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
        latest_user_message=DRAFT_REQUEST,
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
            latest_user_message=DRAFT_REQUEST,
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
            latest_user_message=DRAFT_REQUEST,
            invoke_mcp=changed,
        ))


def test_draft_rejects_unsupported_fact_and_sensitive_content(tmp_path: Path) -> None:
    composition, state, outline = _composition_with_confirmed_outline(tmp_path)

    with pytest.raises(ValueError, match="必须绑定 EvidenceRecord"):
        build_report_draft(
            composition=composition,
            state=state,
            runtime_context=None,
            latest_user_message=DRAFT_REQUEST,
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
            latest_user_message=DRAFT_REQUEST,
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
            latest_user_message=DRAFT_REQUEST,
            proposals=proposals,
        )

    assert composition.repository.list_claim_records(
        tenant_id="tenant-test",
        work_id="work_draft_001",
    ) == ()


def test_outline_confirmation_does_not_advance_to_draft(tmp_path: Path) -> None:
    composition, state, outline = _composition_with_confirmed_outline(tmp_path)
    calls: list[tuple[str, str]] = []

    async def invoke(server: str, tool_name: str, _arguments: dict[str, Any], _confirmed: bool) -> str:
        calls.append((server, tool_name))
        return json.dumps({"chunks": []})

    confirmation = "确认 ReportOutline v1"
    with pytest.raises(ValueError, match="未显式请求生成可审阅初稿"):
        _run(prepare_report_draft_context(
            composition=composition,
            state=state,
            runtime_context=None,
            latest_user_message=confirmation,
            invoke_mcp=invoke,
        ))
    with pytest.raises(ValueError, match="未显式请求生成可审阅初稿"):
        build_report_draft(
            composition=composition,
            state=state,
            runtime_context=None,
            latest_user_message=confirmation,
            proposals=[DraftClaimProposal(
                section_id=outline.sections[0].section_id,
                statement="不应写入。",
                claim_type=ClaimType.RISK,
            )],
        )

    assert calls == []
    assert composition.repository.list_evidence_records(
        tenant_id="tenant-test",
        work_id="work_draft_001",
    ) == ()
    assert composition.repository.list_claim_records(
        tenant_id="tenant-test",
        work_id="work_draft_001",
    ) == ()
    assert composition.repository.get_current_work_contract(
        tenant_id="tenant-test",
        work_id="work_draft_001",
        contract_type=REPORT_DRAFT_CONTRACT_TYPE,
    ) is None


def test_report_draft_confirmation_is_exact_and_idempotent(tmp_path: Path) -> None:
    composition, state, outline = _composition_with_confirmed_outline(tmp_path)

    async def invoke(_server: str, _tool_name: str, _arguments: dict[str, Any], _confirmed: bool) -> str:
        return json.dumps({"chunks": [{"chunk_id": "chunk-1", "content": CONTENT}]})

    context = _run(prepare_report_draft_context(
        composition=composition,
        state=state,
        runtime_context=None,
        latest_user_message=DRAFT_REQUEST,
        invoke_mcp=invoke,
        clock=lambda: NOW,
    ))
    draft = build_report_draft(
        composition=composition,
        state=state,
        runtime_context=None,
        latest_user_message=DRAFT_REQUEST,
        proposals=_proposals(outline, context.evidence[0].evidence_id),
        clock=lambda: NOW,
    )
    saved = composition.save_report_draft(state, None, draft)

    with pytest.raises(WorkCompositionError, match="未显式确认 ReportDraft v1"):
        composition.confirm_report_draft(
            state,
            None,
            expected_version=1,
            latest_user_message="确认",
        )
    with pytest.raises(WorkCompositionError, match="版本不匹配"):
        composition.confirm_report_draft(
            state,
            None,
            expected_version=2,
            latest_user_message="确认 ReportDraft v2",
        )
    assert saved.status is WorkContractStatus.PROVISIONAL

    confirmed = composition.confirm_report_draft(
        state,
        None,
        expected_version=1,
        latest_user_message="确认 ReportDraft v1",
    )
    replay = composition.confirm_report_draft(
        state,
        None,
        expected_version=1,
        latest_user_message="确认 ReportDraft v1",
    )

    assert confirmed == replay
    assert confirmed.status is WorkContractStatus.CONFIRMED
    assert confirmed.confirmed_by == "hmac-" + "2" * 64
    summary = composition.current_work_summary(state)
    assert summary is not None
    draft_summary = cast("dict[str, object]", summary["report_draft"])
    assert draft_summary["status"] == "confirmed"


def test_report_approval_is_exact_bound_idempotent_and_stale_after_revision(tmp_path: Path) -> None:
    composition, state, outline = _composition_with_confirmed_outline(tmp_path)

    async def invoke(_server: str, _tool_name: str, _arguments: dict[str, Any], _confirmed: bool) -> str:
        return json.dumps({"chunks": [{"chunk_id": "chunk-1", "content": CONTENT}]})

    context = _run(prepare_report_draft_context(
        composition=composition,
        state=state,
        runtime_context=None,
        latest_user_message=DRAFT_REQUEST,
        invoke_mcp=invoke,
        clock=lambda: NOW,
    ))
    draft = build_report_draft(
        composition=composition,
        state=state,
        runtime_context=None,
        latest_user_message=DRAFT_REQUEST,
        proposals=_proposals(outline, context.evidence[0].evidence_id),
        clock=lambda: NOW,
    )
    composition.save_report_draft(state, None, draft)

    with pytest.raises(WorkCompositionError, match="没有已确认的 ReportDraft"):
        composition.request_report_approval(
            state,
            None,
            expected_version=1,
            latest_user_message="提交 ReportDraft v1 审批",
        )

    composition.confirm_report_draft(
        state,
        None,
        expected_version=1,
        latest_user_message="确认 ReportDraft v1",
    )
    with pytest.raises(WorkCompositionError, match="未显式提交 ReportDraft v1 审批"):
        composition.request_report_approval(
            state,
            None,
            expected_version=1,
            latest_user_message="请审批初稿",
        )
    pending = composition.request_report_approval(
        state,
        None,
        expected_version=1,
        latest_user_message="提交 ReportDraft v1 审批",
    )
    replay_pending = composition.request_report_approval(
        state,
        None,
        expected_version=1,
        latest_user_message="申请 ReportDraft v1 审批",
    )

    assert pending == replay_pending
    assert pending.contract_type == REPORT_APPROVAL_CONTRACT_TYPE
    assert pending.contract_version == 1
    assert approval_state(pending) == "pending"
    approval = ReportApproval.from_contract(pending)
    assert approval.report_draft_version == 1
    assert approval.report_draft_digest == report_draft_digest(draft)

    with pytest.raises(WorkCompositionError, match="尚未完成内容审批"):
        composition.render_report_artifact(
            state,
            None,
            expected_version=1,
            artifact_format="docx",
            latest_user_message="生成 ReportDraft v1 DOCX",
        )

    with pytest.raises(WorkCompositionError, match="未显式批准 ReportDraft v1"):
        composition.approve_report_draft(
            state,
            None,
            expected_version=1,
            latest_user_message="确认 ReportDraft v1",
        )
    with pytest.raises(WorkCompositionError, match="版本不匹配"):
        composition.approve_report_draft(
            state,
            None,
            expected_version=2,
            latest_user_message="批准 ReportDraft v2",
        )
    approved = composition.approve_report_draft(
        state,
        None,
        expected_version=1,
        latest_user_message="批准 ReportDraft v1",
    )
    replay_approved = composition.approve_report_draft(
        state,
        None,
        expected_version=1,
        latest_user_message="批准 ReportDraft v1",
    )

    assert approved == replay_approved
    assert approval_state(approved) == "approved"
    assert approved.confirmed_by == "hmac-" + "2" * 64
    item = composition.current_work(state)
    assert item is not None
    assert item.approval_state == "not_requested"
    assert item.artifact_ids == ()
    summary = composition.current_work_summary(state)
    assert summary is not None
    assert summary["report_approval"] == {
        "contract_version": 1,
        "status": "approved",
        "report_draft_version": 1,
        "policy_id": "industry-report-v1",
        "current": True,
    }

    with pytest.raises(WorkCompositionError, match="未显式请求生成"):
        composition.render_report_artifact(
            state,
            None,
            expected_version=1,
            artifact_format="docx",
            latest_user_message="批准 ReportDraft v1",
        )
    artifact = composition.render_report_artifact(
        state,
        None,
        expected_version=1,
        artifact_format="docx",
        latest_user_message="生成 ReportDraft v1 DOCX",
    )
    item_after_render = composition.current_work(state)
    assert item_after_render is not None
    version_after_render = item_after_render.version
    artifact_events_after_render = tuple(
        event
        for event in composition.repository.list_events(
            tenant_id="tenant-test",
            work_id="work_draft_001",
        )
        if event.event_type == "artifact_registered"
    )
    composition._factory = replace(
        composition._factory,
        clock=lambda: NOW + timedelta(minutes=5),
    )
    replay_artifact = composition.render_report_artifact(
        state,
        None,
        expected_version=1,
        artifact_format="docx",
        latest_user_message="导出 ReportDraft v1 Word 文档",
    )

    assert artifact == replay_artifact
    assert replay_artifact.created_at == NOW
    assert len(composition.repository.list_artifact_records(
        tenant_id="tenant-test",
        work_id="work_draft_001",
    )) == 1
    replay_events = tuple(
        event
        for event in composition.repository.list_events(
            tenant_id="tenant-test",
            work_id="work_draft_001",
        )
        if event.event_type == "artifact_registered"
    )
    assert replay_events == artifact_events_after_render
    item_after_replay = composition.current_work(state)
    assert item_after_replay is not None
    assert item_after_replay.version == version_after_render
    assert artifact.source_contract_version == 1
    assert artifact.approval_contract_version == 1
    assert artifact.metadata["publication_status"] == "not_published"
    assert artifact.metadata["delivery_status"] == "not_delivered"
    artifact_path = composition.artifact_store.resolve(artifact.storage_key)
    assert artifact_path.is_file()
    assert artifact.content_sha256 == f"sha256:{sha256(artifact_path.read_bytes()).hexdigest()}"
    assert not artifact.storage_key.startswith("/")
    stored_item = composition.current_work(state)
    assert stored_item is not None
    assert stored_item.artifact_ids == (artifact.artifact_id,)
    artifact_summary = composition.current_work_summary(state)
    assert artifact_summary is not None
    artifacts = cast("list[dict[str, object]]", artifact_summary["report_artifacts"])
    assert artifacts[0]["current"] is True

    with pytest.raises(WorkCompositionError, match="尚未形成唯一正式发布记录"):
        composition.prepare_report_delivery(
            state,
            None,
            expected_version=1,
            latest_user_message="交付 ReportArtifact v1 给我",
        )

    with pytest.raises(WorkCompositionError, match="未精确请求发布"):
        composition.publish_report_artifact(
            state,
            None,
            expected_version=1,
            latest_user_message="请发布 ReportArtifact v1",
        )
    with pytest.raises(WorkCompositionError, match="版本不匹配"):
        composition.publish_report_artifact(
            state,
            None,
            expected_version=2,
            latest_user_message="发布 ReportArtifact v2",
        )
    publication = composition.publish_report_artifact(
        state,
        None,
        expected_version=1,
        latest_user_message="发布 ReportArtifact v1",
    )
    published_item = composition.current_work(state)
    assert published_item is not None
    published_version = published_item.version
    replay_publication = composition.publish_report_artifact(
        state,
        None,
        expected_version=1,
        latest_user_message="发布 ReportArtifact v1",
    )

    assert publication == replay_publication
    assert publication.publication_version == 1
    assert publication.artifact_id == artifact.artifact_id
    assert publication.content_sha256 == artifact.content_sha256
    assert publication.metadata["delivery_status"] == "not_delivered"
    replay_item = composition.current_work(state)
    assert replay_item is not None
    assert replay_item.status.value == "published"
    assert replay_item.version == published_version
    publication_summary = composition.current_work_summary(state)
    assert publication_summary is not None
    publications = cast("list[dict[str, object]]", publication_summary["report_publications"])
    assert publications[0]["current"] is True
    assert publications[0]["delivery_status"] == "not_delivered"
    assert cast("list[dict[str, object]]", publication_summary["report_artifacts"])[0][
        "publication_status"
    ] == "published"

    with pytest.raises(WorkCompositionError, match="未精确请求"):
        composition.prepare_report_delivery(
            state,
            None,
            expected_version=1,
            latest_user_message="请交付 ReportArtifact v1 给我",
        )
    prepared = composition.prepare_report_delivery(
        state,
        None,
        expected_version=1,
        latest_user_message=(
            "from_userid=opaque|channel=$wecom|chat_id=opaque\n"
            "---Date: 2026-07-20T09:00:00+08:00---\n"
            "交付 ReportArtifact v1 给我"
        ),
    )
    assert prepared.download_url.startswith(
        f"https://reports.example.test/artifacts/{prepared.record.delivery_id}#"
    )
    assert prepared.grant_token not in prepared.record.grant_hash
    delivery = composition.commit_report_delivery(prepared)
    assert delivery.artifact_id == artifact.artifact_id
    delivered_item = composition.current_work(state)
    assert delivered_item is not None
    assert delivered_item.status.value == "delivered"
    active_replay = composition.prepare_report_delivery(
        state,
        None,
        expected_version=1,
        latest_user_message="交付 ReportArtifact v1 给我",
    )
    assert active_replay.already_delivered is True
    assert active_replay.record.delivery_id == delivery.delivery_id
    assert active_replay.grant_token == ""
    download = composition.redeem_report_delivery(delivery.delivery_id, prepared.grant_token)
    assert download.data == artifact_path.read_bytes()
    assert download.filename == artifact.filename
    with pytest.raises(ArtifactDownloadGone):
        composition.redeem_report_delivery(delivery.delivery_id, prepared.grant_token)
    consumed_reissue = composition.prepare_report_delivery(
        state,
        None,
        expected_version=1,
        latest_user_message="交付 ReportArtifact v1 给我",
    )
    assert consumed_reissue.already_delivered is False
    assert consumed_reissue.record.delivery_version == 2
    assert consumed_reissue.record.delivery_id != delivery.delivery_id
    assert consumed_reissue.grant_token
    assert consumed_reissue.grant_token != prepared.grant_token
    assert consumed_reissue.grant_token not in consumed_reissue.record.grant_hash
    delivery_summary = composition.current_work_summary(state)
    assert delivery_summary is not None
    deliveries = cast("list[dict[str, object]]", delivery_summary["report_deliveries"])
    assert deliveries == [{
        "delivery_id": delivery.delivery_id,
        "delivery_version": 1,
        "status": "delivered",
        "artifact_id": artifact.artifact_id,
        "publication_id": publication.publication_id,
        "content_sha256": artifact.content_sha256,
        "report_draft_version": 1,
        "current": True,
        "grant_state": "consumed",
    }]

    revised_draft = replace(draft, markdown=f"{draft.markdown}\n\n修订说明。")
    revised = composition.save_report_draft(state, None, revised_draft)
    assert revised.contract_version == 2
    stale_summary = composition.current_work_summary(state)
    assert stale_summary is not None
    assert cast("dict[str, object]", stale_summary["report_approval"])["current"] is False
    stale_artifacts = cast("list[dict[str, object]]", stale_summary["report_artifacts"])
    assert stale_artifacts[0]["current"] is False
    stale_publications = cast("list[dict[str, object]]", stale_summary["report_publications"])
    assert stale_publications[0]["current"] is False
    stale_deliveries = cast("list[dict[str, object]]", stale_summary["report_deliveries"])
    assert stale_deliveries[0]["current"] is False
    composition.confirm_report_draft(
        state,
        None,
        expected_version=2,
        latest_user_message="确认 ReportDraft v2",
    )
    with pytest.raises(WorkCompositionError, match="版本不匹配"):
        composition.prepare_report_delivery(
            state,
            None,
            expected_version=1,
            latest_user_message="交付 ReportArtifact v1 给我",
        )
    with pytest.raises(WorkCompositionError, match="失效"):
        composition.render_report_artifact(
            state,
            None,
            expected_version=2,
            artifact_format="docx",
            latest_user_message="生成 ReportDraft v2 DOCX",
        )
    pending_v2 = composition.request_report_approval(
        state,
        None,
        expected_version=2,
        latest_user_message="提交 ReportDraft v2 审批",
    )
    assert pending_v2.contract_version == 2
    assert approval_state(pending_v2) == "pending"
    approvals = composition.repository.list_work_contracts(
        tenant_id="tenant-test",
        work_id="work_draft_001",
        contract_type=REPORT_APPROVAL_CONTRACT_TYPE,
    )
    assert [contract.status for contract in approvals] == [
        WorkContractStatus.SUPERSEDED,
        WorkContractStatus.PROVISIONAL,
    ]
    approved_v2 = composition.approve_report_draft(
        state,
        None,
        expected_version=2,
        latest_user_message="批准 ReportDraft v2",
    )
    assert approval_state(approved_v2) == "approved"
    current_summary = composition.current_work_summary(state)
    assert current_summary is not None
    assert cast("dict[str, object]", current_summary["report_approval"])["current"] is True

    composition.save_report_brief(
        state,
        None,
        ReportBrief(
            title="证券行业数字化转型报告",
            target_audience=("公司管理层",),
            coverage_period="2027年全年",
        ),
    )
    upstream_stale_summary = composition.current_work_summary(state)
    assert upstream_stale_summary is not None
    assert cast("dict[str, object]", upstream_stale_summary["report_approval"])["current"] is False


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
        artifact_store_root=tmp_path / "artifacts",
        template_asset_path=PACK_ROOT / "assets" / "neutral-industry-report-v1.docx",
        artifact_public_base_url="https://reports.example.test/artifacts",
        artifact_grant_ttl_seconds=3600,
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
