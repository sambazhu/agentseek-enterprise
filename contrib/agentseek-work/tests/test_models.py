from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from agentseek_work.models import (
    ClaimRecord,
    ClaimReviewerStatus,
    ClaimType,
    ClaimVerificationStatus,
    EvidenceRecord,
    ExcerptStatus,
    PackSnapshot,
    PublicationRecord,
    PublicationStatus,
    SnapshotStatus,
    SourceRecord,
    SourceType,
    WorkBudget,
    WorkContractSnapshot,
    WorkContractStatus,
    WorkItem,
    WorkStatus,
)

NOW = datetime(2026, 7, 12, tzinfo=UTC)


def make_item() -> WorkItem:
    return WorkItem(
        work_id="work_001",
        tenant_id="tenant_001",
        digital_employee_id="industry-report",
        pack_id="industry-report",
        pack_version="1.0.0",
        pack_snapshot_id="sha256:pack",
        runtime_release="enterprise-wecom-v0.1.0-alpha1",
        requester_id="employee_001",
        reviewer_id="employee_001",
        approver_id="employee_001",
        data_owner_id="employee_001",
        beneficiary_id="employee_001",
        playbook_id="securities_industry_report",
        playbook_version="1",
        budget_id="budget_001",
        idempotency_key="tenant_001:request_001",
        created_at=NOW,
        updated_at=NOW,
    )


def test_work_item_defaults_to_non_terminal_draft() -> None:
    item = replace(make_item(), brief={"title": "2025年中国证券行业发展研究报告"})

    assert item.status is WorkStatus.DRAFT
    assert item.version == 0
    assert item.phase_attempt == 0
    assert item.brief["title"] == "2025年中国证券行业发展研究报告"
    assert not item.is_terminal


def test_work_item_copies_brief_into_read_only_mapping() -> None:
    brief = {"title": "before"}
    item = replace(make_item(), brief=brief)
    brief["title"] = "after"

    assert item.brief["title"] == "before"
    with pytest.raises(TypeError):
        cast(Any, item.brief)["title"] = "blocked"


def test_work_item_rejects_naive_timestamps() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(make_item(), created_at=datetime(2026, 7, 12), updated_at=datetime(2026, 7, 12))


def test_work_budget_rejects_phase_duration_above_work_duration() -> None:
    with pytest.raises(ValueError, match="must not exceed"):
        WorkBudget(
            max_model_calls=10,
            max_input_tokens=1000,
            max_output_tokens=1000,
            max_external_queries=10,
            max_phase_duration_seconds=301,
            max_work_duration_seconds=300,
            max_retry_count=2,
        )


def test_pack_snapshot_is_immutable_and_rejects_duplicate_assets() -> None:
    with pytest.raises(ValueError, match="duplicates"):
        PackSnapshot(
            pack_snapshot_id="pack_snapshot_sha256_abc",
            pack_id="industry-report",
            pack_version="1.0.0",
            manifest_digest="sha256:manifest",
            content_artifact_id="pack-content://sha256/content",
            asset_version_refs=("asset@1", "asset@1"),
            created_at=NOW,
        )


def test_work_contract_copies_payload_and_enforces_lifecycle() -> None:
    payload = {"title": "before"}
    contract = WorkContractSnapshot(
        work_id="work_001",
        tenant_id="tenant_001",
        contract_type="report-brief",
        contract_version=1,
        status=WorkContractStatus.PROVISIONAL,
        payload=payload,
        created_by="employee_001",
        created_at=NOW,
    )
    payload["title"] = "after"

    assert contract.payload["title"] == "before"
    assert contract.is_current
    with pytest.raises(TypeError):
        cast(Any, contract.payload)["title"] = "blocked"
    with pytest.raises(ValueError, match="requires confirmation"):
        replace(contract, status=WorkContractStatus.CONFIRMED)
    with pytest.raises(ValueError, match="requires superseded_at"):
        replace(contract, status=WorkContractStatus.SUPERSEDED)


def test_work_contract_confirmation_fields_are_paired_and_aware() -> None:
    contract = WorkContractSnapshot(
        work_id="work_001",
        tenant_id="tenant_001",
        contract_type="report-brief",
        contract_version=1,
        status=WorkContractStatus.CONFIRMED,
        payload={"title": "report"},
        created_by="employee_001",
        created_at=NOW,
        confirmed_by="employee_001",
        confirmed_at=NOW,
    )

    assert contract.status is WorkContractStatus.CONFIRMED
    with pytest.raises(ValueError, match="set together"):
        replace(contract, confirmed_by=None)
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(contract, confirmed_at=datetime(2026, 7, 12))


def test_source_record_is_immutable_and_validates_provenance() -> None:
    metadata = {"section_ids": ["industry-overview"]}
    source = SourceRecord(
        source_id="source_sha256_abc",
        work_id="work_001",
        tenant_id="tenant_001",
        source_type=SourceType.DEPARTMENT_KNOWLEDGE,
        title="证券行业规划",
        publisher="战略发展部",
        retrieved_at=NOW,
        locator="mcp://department-knowledge/doc-1#chunk-1",
        uri_digest="sha256:uri",
        content_hash="sha256:content",
        result_digest="sha256:result",
        confidentiality_level="internal",
        authority_level="approved_internal",
        allowed_uses=("research", "citation"),
        snapshot_policy="reference_only",
        snapshot_status=SnapshotStatus.REFERENCED,
        retrieval_query_digest="sha256:query",
        excerpt_status=ExcerptStatus.STORED,
        license_terms_ref="internal-policy://department-knowledge/v1",
        metadata=metadata,
    )
    metadata["section_ids"] = []

    assert source.metadata["section_ids"] == ["industry-overview"]
    with pytest.raises(TypeError):
        cast(Any, source.metadata)["changed"] = True
    with pytest.raises(ValueError, match="duplicates"):
        replace(source, allowed_uses=("research", "research"))
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(source, retrieved_at=datetime(2026, 7, 12))


def test_evidence_and_claim_records_validate_governed_bindings() -> None:
    evidence = EvidenceRecord(
        evidence_id="evidence_sha256_abc",
        work_id="work_001",
        tenant_id="tenant_001",
        source_id="source_sha256_abc",
        locator="mcp://department-knowledge/doc-1#chunk-1",
        excerpt="证券行业数字化转型应提升客户服务和风险控制能力。",
        confidence=0.9,
        extraction_method="department_knowledge_chunk",
        created_at=NOW,
        metadata={"question_ids": ["q1"]},
    )
    claim = ClaimRecord(
        claim_id="claim_sha256_abc",
        work_id="work_001",
        tenant_id="tenant_001",
        section_id="executive-summary",
        statement="数字化转型应同时提升客户服务和风险控制能力。",
        claim_type=ClaimType.FACT,
        evidence_ids=(evidence.evidence_id,),
        verification_status=ClaimVerificationStatus.VERIFIED,
        reviewer_status=ClaimReviewerStatus.PENDING,
        created_at=NOW,
    )

    assert evidence.metadata["question_ids"] == ["q1"]
    assert claim.evidence_ids == (evidence.evidence_id,)
    with pytest.raises(ValueError, match="requires an excerpt"):
        replace(evidence, excerpt=None)
    with pytest.raises(ValueError, match="requires evidence_ids"):
        replace(claim, evidence_ids=())


def test_publication_record_is_immutable_and_validates_exact_bindings() -> None:
    publication = PublicationRecord(
        publication_id="publication_sha256_abc",
        publication_version=1,
        work_id="work_001",
        tenant_id="tenant_001",
        artifact_id="artifact_sha256_abc",
        content_sha256=f"sha256:{'a' * 64}",
        source_contract_version=1,
        approval_contract_version=1,
        template_digest=f"sha256:{'b' * 64}",
        policy_id="industry-report-v1",
        status=PublicationStatus.PUBLISHED,
        published_by="employee_001",
        published_at=NOW,
        metadata={"delivery_status": "not_delivered"},
    )

    assert publication.metadata["delivery_status"] == "not_delivered"
    with pytest.raises(TypeError):
        cast(Any, publication.metadata)["changed"] = True
    with pytest.raises(ValueError, match="greater than zero"):
        replace(publication, publication_version=0)
    with pytest.raises(ValueError, match="canonical sha256"):
        replace(publication, content_sha256="sha256:invalid")
