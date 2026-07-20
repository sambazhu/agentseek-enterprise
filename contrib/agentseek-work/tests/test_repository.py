from dataclasses import replace
from datetime import UTC, datetime, timedelta

import agentseek_work.repository as repository_module
import pytest
from agentseek_work.migrations import LATEST_SCHEMA_VERSION, apply_migrations
from agentseek_work.models import (
    ActorType,
    ArtifactRecord,
    ClaimRecord,
    ClaimReviewerStatus,
    ClaimType,
    ClaimVerificationStatus,
    DeliveryRecord,
    DeliveryStatus,
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
from agentseek_work.repository import (
    ActiveWorkConflictError,
    DeliveryGrantConsumedError,
    DeliveryGrantExpiredError,
    DeliveryGrantNotFoundError,
    NonJsonValueError,
    SQLAlchemyWorkRepository,
    WorkConflictError,
    WorkContractConflictError,
    WorkNotFoundError,
)
from agentseek_work.schema import (
    schema_versions,
    work_artifacts,
    work_claim_evidence,
    work_claims,
    work_contracts,
    work_deliveries,
    work_evidence,
    work_publications,
    work_sources,
)
from agentseek_work.state_machine import OptimisticConcurrencyError, transition_work_item
from sqlalchemy import Connection, create_engine, insert, inspect
from sqlalchemy.exc import IntegrityError

NOW = datetime(2026, 7, 12, tzinfo=UTC)


def make_budget() -> WorkBudget:
    return WorkBudget(
        max_model_calls=20,
        max_input_tokens=100_000,
        max_output_tokens=30_000,
        max_external_queries=50,
        max_phase_duration_seconds=600,
        max_work_duration_seconds=3000,
        max_retry_count=2,
    )


def make_item(
    *,
    work_id: str = "work_001",
    tenant_id: str = "tenant_001",
    idempotency_key: str = "request_001",
    requester_id: str = "employee_001",
    digital_employee_id: str = "industry-report",
    playbook_id: str = "securities_industry_report",
) -> WorkItem:
    return WorkItem(
        work_id=work_id,
        tenant_id=tenant_id,
        digital_employee_id=digital_employee_id,
        pack_id="industry-report",
        pack_version="1.0.0",
        pack_snapshot_id="sha256:pack",
        runtime_release="enterprise-wecom-v0.1.0-alpha1",
        requester_id=requester_id,
        reviewer_id=requester_id,
        approver_id=requester_id,
        data_owner_id=requester_id,
        beneficiary_id=requester_id,
        playbook_id=playbook_id,
        playbook_version="1",
        budget_id="budget_001",
        idempotency_key=idempotency_key,
        created_at=NOW,
        updated_at=NOW,
        brief={"title": "2025年中国证券行业发展研究报告"},
        input_file_ids=("file_001",),
        skill_digests=("sha256:skill",),
    )


@pytest.fixture
def repository() -> SQLAlchemyWorkRepository:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    assert apply_migrations(engine) == LATEST_SCHEMA_VERSION
    repo = SQLAlchemyWorkRepository(engine)
    repo.put_budget("budget_001", make_budget())
    repo.put_pack_snapshot(make_pack_snapshot())
    return repo


def make_pack_snapshot() -> PackSnapshot:
    return PackSnapshot(
        pack_snapshot_id="sha256:pack",
        pack_id="industry-report",
        pack_version="1.0.0",
        manifest_digest="sha256:manifest",
        content_artifact_id="pack-content://sha256/content",
        asset_version_refs=(),
        created_at=NOW,
    )


def make_contract(
    *,
    version: int = 1,
    title: str = "2025年中国证券行业发展研究报告",
    created_at: datetime = NOW,
) -> WorkContractSnapshot:
    return WorkContractSnapshot(
        work_id="work_001",
        tenant_id="tenant_001",
        contract_type="report-brief",
        contract_version=version,
        status=WorkContractStatus.PROVISIONAL,
        payload={"title": title},
        created_by="employee_001",
        created_at=created_at,
    )


def make_source(*, tenant_id: str = "tenant_001", work_id: str = "work_001") -> SourceRecord:
    return SourceRecord(
        source_id="source_sha256_abc",
        work_id=work_id,
        tenant_id=tenant_id,
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
        metadata={"section_ids": ["industry-overview"]},
    )


def make_evidence(*, tenant_id: str = "tenant_001", work_id: str = "work_001") -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id="evidence_sha256_abc",
        work_id=work_id,
        tenant_id=tenant_id,
        source_id="source_sha256_abc",
        locator="mcp://department-knowledge/doc-1#chunk-1",
        excerpt="证券行业数字化转型应提升客户服务和风险控制能力。",
        confidence=0.9,
        extraction_method="department_knowledge_chunk",
        created_at=NOW,
        metadata={"question_ids": ["q1"]},
    )


def make_claim(*, tenant_id: str = "tenant_001", work_id: str = "work_001") -> ClaimRecord:
    return ClaimRecord(
        claim_id="claim_sha256_abc",
        work_id=work_id,
        tenant_id=tenant_id,
        section_id="executive-summary",
        statement="数字化转型应同时提升客户服务和风险控制能力。",
        claim_type=ClaimType.FACT,
        evidence_ids=("evidence_sha256_abc",),
        verification_status=ClaimVerificationStatus.VERIFIED,
        reviewer_status=ClaimReviewerStatus.PENDING,
        created_at=NOW,
        metadata={"report_outline_version": 1},
    )


def make_artifact(*, tenant_id: str = "tenant_001", work_id: str = "work_001") -> ArtifactRecord:
    digest = f"sha256:{'a' * 64}"
    return ArtifactRecord(
        artifact_id=f"artifact_{'b' * 64}",
        work_id=work_id,
        tenant_id=tenant_id,
        artifact_type="report",
        artifact_format="docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        content_sha256=digest,
        size_bytes=4096,
        storage_key=f"{tenant_id}/{work_id}/docx/{'a' * 64}.docx",
        filename="industry-report-draft-v1.docx",
        source_contract_type="report-draft",
        source_contract_version=1,
        source_digest=digest,
        approval_contract_version=1,
        approval_digest=f"sha256:{'c' * 64}",
        template_ref="trusted-asset://strategic-report-docx/1.0.0",
        template_digest=f"sha256:{'d' * 64}",
        created_by="employee_001",
        created_at=NOW,
        metadata={"publication_status": "not_published"},
    )


def make_publication(
    artifact: ArtifactRecord | None = None,
    *,
    version: int = 1,
    published_at: datetime = NOW + timedelta(seconds=2),
) -> PublicationRecord:
    current = artifact or make_artifact()
    return PublicationRecord(
        publication_id=f"publication_{'e' * 64}",
        publication_version=version,
        work_id=current.work_id,
        tenant_id=current.tenant_id,
        artifact_id=current.artifact_id,
        content_sha256=current.content_sha256,
        source_contract_version=current.source_contract_version,
        approval_contract_version=current.approval_contract_version,
        template_digest=current.template_digest,
        policy_id="industry-report-v1",
        status=PublicationStatus.PUBLISHED,
        published_by="employee_001",
        published_at=published_at,
        metadata={"delivery_status": "not_delivered"},
    )


def make_delivery(
    publication: PublicationRecord | None = None,
    *,
    version: int = 1,
    delivered_at: datetime = NOW + timedelta(seconds=3),
) -> DeliveryRecord:
    current = publication or make_publication()
    return DeliveryRecord(
        delivery_id=f"delivery_{'f' * 64}",
        delivery_version=version,
        work_id=current.work_id,
        tenant_id=current.tenant_id,
        artifact_id=current.artifact_id,
        publication_id=current.publication_id,
        content_sha256=current.content_sha256,
        size_bytes=4096,
        recipient_key="employee_001",
        grant_hash=f"sha256:{'9' * 64}",
        grant_expires_at=delivered_at + timedelta(minutes=30),
        status=DeliveryStatus.DELIVERED,
        delivered_by="employee_001",
        delivered_at=delivered_at,
        metadata={"delivery_mode": "signed_link"},
    )


def transition(item: WorkItem, to_status: WorkStatus, *, event_id: str):
    return transition_work_item(
        item,
        to_status=to_status,
        expected_version=item.version,
        event_id=event_id,
        event_type="status_changed",
        actor_type=ActorType.SYSTEM,
        actor_id="worker_001",
        occurred_at=NOW + timedelta(seconds=item.version + 1),
        payload_digest="sha256:payload",
        policy_decision="allowed",
    )


def _create_legacy_work_items_table(connection: Connection) -> None:
    connection.exec_driver_sql(
        "CREATE TABLE enterprise_work_items ("
        "work_id VARCHAR(128) PRIMARY KEY, "
        "tenant_id VARCHAR(128) NOT NULL, "
        "requester_id VARCHAR(128) NOT NULL, "
        "digital_employee_id VARCHAR(128) NOT NULL, "
        "playbook_id VARCHAR(128) NOT NULL, "
        "status VARCHAR(32) NOT NULL"
        ")"
    )


def test_migration_is_idempotent() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    assert apply_migrations(engine) == LATEST_SCHEMA_VERSION
    assert apply_migrations(engine) == LATEST_SCHEMA_VERSION


def test_migration_rejects_newer_database_version() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    apply_migrations(engine)
    with engine.begin() as connection:
        connection.execute(insert(schema_versions).values(version=LATEST_SCHEMA_VERSION + 1, applied_at=NOW))

    with pytest.raises(RuntimeError, match="newer than supported"):
        apply_migrations(engine)


def test_revision_four_adds_profile_audit_columns_to_revision_three_database() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE enterprise_work_schema_versions (version INTEGER PRIMARY KEY, applied_at DATETIME NOT NULL)"
        )
        _create_legacy_work_items_table(connection)
        connection.execute(insert(schema_versions).values(version=3, applied_at=NOW))

    assert apply_migrations(engine) == LATEST_SCHEMA_VERSION
    columns = {column["name"] for column in inspect(engine).get_columns("enterprise_work_items")}
    assert "digital_employee_profile_version" in columns
    assert "digital_employee_permissions_digest" in columns


def test_revision_five_creates_active_playbook_unique_index() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE enterprise_work_schema_versions (version INTEGER PRIMARY KEY, applied_at DATETIME NOT NULL)"
        )
        _create_legacy_work_items_table(connection)
        connection.execute(insert(schema_versions).values(version=4, applied_at=NOW))

    assert apply_migrations(engine) == LATEST_SCHEMA_VERSION
    indexes = {index["name"]: index for index in inspect(engine).get_indexes("enterprise_work_items")}
    assert indexes["uq_work_items_active_playbook"]["unique"] == 1
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO enterprise_work_items "
            "(work_id, tenant_id, requester_id, digital_employee_id, playbook_id, status) VALUES "
            "('terminal_1', 'tenant', 'requester', 'employee', 'playbook', 'succeeded'), "
            "('terminal_2', 'tenant', 'requester', 'employee', 'playbook', 'failed'), "
            "('active_1', 'tenant', 'requester', 'employee', 'playbook', 'draft')"
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO enterprise_work_items "
            "(work_id, tenant_id, requester_id, digital_employee_id, playbook_id, status) VALUES "
            "('active_2', 'tenant', 'requester', 'employee', 'playbook', 'running')"
        )


def test_revision_five_fails_closed_when_active_scopes_are_duplicated() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE enterprise_work_schema_versions (version INTEGER PRIMARY KEY, applied_at DATETIME NOT NULL)"
        )
        _create_legacy_work_items_table(connection)
        connection.exec_driver_sql(
            "INSERT INTO enterprise_work_items "
            "(work_id, tenant_id, requester_id, digital_employee_id, playbook_id, status) VALUES "
            "('work_1', 'tenant', 'requester', 'employee', 'playbook', 'draft'), "
            "('work_2', 'tenant', 'requester', 'employee', 'playbook', 'running')"
        )
        connection.execute(insert(schema_versions).values(version=4, applied_at=NOW))

    with pytest.raises(RuntimeError, match=r"active WorkItem scope.*duplicates"):
        apply_migrations(engine)
    indexes = {index["name"] for index in inspect(engine).get_indexes("enterprise_work_items")}
    assert "uq_work_items_active_playbook" not in indexes


def test_revision_six_creates_current_contract_unique_index() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    assert apply_migrations(engine) == LATEST_SCHEMA_VERSION
    indexes = {index["name"]: index for index in inspect(engine).get_indexes(work_contracts.name)}

    assert indexes["uq_work_contracts_current_type"]["unique"] == 1
    base_values = {
        "work_id": "work_1",
        "tenant_id": "tenant",
        "contract_type": "report-brief",
        "payload": {"title": "report"},
        "created_by": "requester",
        "created_at": NOW,
    }
    with engine.begin() as connection:
        connection.execute(
            insert(work_contracts).values(
                **base_values,
                contract_version=1,
                status=WorkContractStatus.PROVISIONAL.value,
            )
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            insert(work_contracts).values(
                **base_values,
                contract_version=2,
                status=WorkContractStatus.PROVISIONAL.value,
            )
        )


def test_revision_six_fails_closed_when_current_contracts_are_duplicated() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE enterprise_work_schema_versions (version INTEGER PRIMARY KEY, applied_at DATETIME NOT NULL)"
        )
        _create_legacy_work_items_table(connection)
        connection.exec_driver_sql(
            "CREATE TABLE enterprise_work_contracts ("
            "work_id VARCHAR(128) NOT NULL, "
            "tenant_id VARCHAR(128) NOT NULL, "
            "contract_type VARCHAR(128) NOT NULL, "
            "contract_version INTEGER NOT NULL, "
            "status VARCHAR(32) NOT NULL, "
            "payload JSON NOT NULL, "
            "created_by VARCHAR(128) NOT NULL, "
            "created_at DATETIME NOT NULL, "
            "confirmed_by VARCHAR(128), "
            "confirmed_at DATETIME, "
            "superseded_at DATETIME, "
            "PRIMARY KEY (work_id, contract_type, contract_version)"
            ")"
        )
        connection.exec_driver_sql(
            "INSERT INTO enterprise_work_contracts "
            "(work_id, tenant_id, contract_type, contract_version, status, payload, created_by, created_at) VALUES "
            "('work_1', 'tenant', 'report-brief', 1, 'provisional', '{}', 'requester', '2026-07-14'), "
            "('work_1', 'tenant', 'report-brief', 2, 'provisional', '{}', 'requester', '2026-07-14')"
        )
        connection.execute(insert(schema_versions).values(version=5, applied_at=NOW))

    with pytest.raises(RuntimeError, match="multiple current versions"):
        apply_migrations(engine)
    indexes = {index["name"] for index in inspect(engine).get_indexes(work_contracts.name)}
    assert "uq_work_contracts_current_type" not in indexes


def test_revision_seven_creates_source_ledger_and_fails_closed_on_partial_table() -> None:
    clean = create_engine("sqlite+pysqlite:///:memory:")
    assert apply_migrations(clean) == LATEST_SCHEMA_VERSION
    columns = {column["name"] for column in inspect(clean).get_columns(work_sources.name)}
    assert {"source_id", "work_id", "tenant_id", "result_digest", "metadata"} <= columns

    partial = create_engine("sqlite+pysqlite:///:memory:")
    with partial.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE enterprise_work_schema_versions (version INTEGER PRIMARY KEY, applied_at DATETIME NOT NULL)"
        )
        _create_legacy_work_items_table(connection)
        connection.exec_driver_sql(
            "CREATE TABLE enterprise_work_sources ("
            "source_id VARCHAR(160) PRIMARY KEY, work_id VARCHAR(128), tenant_id VARCHAR(128))"
        )
        connection.execute(insert(schema_versions).values(version=6, applied_at=NOW))

    with pytest.raises(RuntimeError, match=r"revision 7.*is missing"):
        apply_migrations(partial)


def test_revision_eight_creates_evidence_claim_and_binding_tables() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    assert apply_migrations(engine) == LATEST_SCHEMA_VERSION
    table_names = set(inspect(engine).get_table_names())
    assert {work_evidence.name, work_claims.name, work_claim_evidence.name} <= table_names


def test_revision_nine_creates_artifact_ledger() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    assert apply_migrations(engine) == LATEST_SCHEMA_VERSION
    assert work_artifacts.name in set(inspect(engine).get_table_names())


def test_revision_ten_creates_publication_ledger() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    assert apply_migrations(engine) == LATEST_SCHEMA_VERSION
    assert work_publications.name in set(inspect(engine).get_table_names())


def test_revision_eleven_creates_delivery_ledger() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    assert apply_migrations(engine) == LATEST_SCHEMA_VERSION
    assert work_deliveries.name in set(inspect(engine).get_table_names())
    columns = {column["name"] for column in inspect(engine).get_columns(work_deliveries.name)}
    assert {
        "delivery_id",
        "publication_id",
        "recipient_key",
        "grant_hash",
        "grant_expires_at",
        "grant_consumed_at",
    } <= columns


def test_create_is_idempotent_within_tenant(repository: SQLAlchemyWorkRepository) -> None:
    original = make_item()
    first = repository.create_work(original)
    replay = repository.create_work(make_item(work_id="work_replayed", idempotency_key=original.idempotency_key))

    assert first.created
    assert not replay.created
    assert replay.item.work_id == original.work_id


def test_source_record_round_trip_is_idempotent_tenant_scoped_and_immutable(
    repository: SQLAlchemyWorkRepository,
) -> None:
    repository.create_work(make_item())
    source = make_source()

    assert repository.put_source_record(source) == source
    assert repository.put_source_record(source) == source
    assert repository.get_source_record(tenant_id=source.tenant_id, source_id=source.source_id) == source
    assert repository.list_source_records(tenant_id=source.tenant_id, work_id=source.work_id) == (source,)
    with pytest.raises(WorkConflictError, match="different values"):
        repository.put_source_record(replace(source, title="changed"))
    with pytest.raises(WorkNotFoundError):
        repository.get_source_record(tenant_id="tenant_other", source_id=source.source_id)
    with pytest.raises(WorkNotFoundError):
        repository.put_source_record(replace(source, source_id="source_other", tenant_id="tenant_other"))


def test_evidence_and_claim_records_are_immutable_scoped_and_idempotent(
    repository: SQLAlchemyWorkRepository,
) -> None:
    repository.create_work(make_item())
    source = repository.put_source_record(make_source())
    evidence = make_evidence()
    claim = make_claim()

    assert repository.put_evidence_record(evidence) == evidence
    assert repository.put_evidence_record(evidence) == evidence
    assert repository.get_evidence_record(tenant_id=evidence.tenant_id, evidence_id=evidence.evidence_id) == evidence
    assert repository.list_evidence_records(tenant_id=evidence.tenant_id, work_id=evidence.work_id) == (evidence,)
    assert repository.put_claim_record(claim) == claim
    assert repository.put_claim_record(claim) == claim
    assert repository.get_claim_record(tenant_id=claim.tenant_id, claim_id=claim.claim_id) == claim
    assert repository.list_claim_records(tenant_id=claim.tenant_id, work_id=claim.work_id) == (claim,)

    with pytest.raises(WorkConflictError, match="different values"):
        repository.put_evidence_record(replace(evidence, excerpt="changed"))
    with pytest.raises(WorkConflictError, match="different values"):
        repository.put_claim_record(replace(claim, statement="changed"))
    repository.create_work(
        make_item(work_id="work_002", idempotency_key="request_002", requester_id="employee_002")
    )
    with pytest.raises(WorkConflictError, match="different WorkItem"):
        repository.put_evidence_record(replace(evidence, evidence_id="evidence_other", work_id="work_002"))
    assert source.source_id == evidence.source_id


def test_artifact_record_is_immutable_scoped_idempotent_and_attached(
    repository: SQLAlchemyWorkRepository,
) -> None:
    repository.create_work(make_item())
    artifact = make_artifact()

    assert repository.put_artifact_record(artifact) == artifact
    assert repository.put_artifact_record(artifact) == artifact
    assert repository.get_artifact_record(tenant_id=artifact.tenant_id, artifact_id=artifact.artifact_id) == artifact
    assert repository.list_artifact_records(tenant_id=artifact.tenant_id, work_id=artifact.work_id) == (artifact,)
    item = repository.get_work(tenant_id=artifact.tenant_id, work_id=artifact.work_id)
    assert item.artifact_ids == (artifact.artifact_id,)
    assert item.version == 1
    events = repository.list_events(tenant_id=artifact.tenant_id, work_id=artifact.work_id)
    assert [(event.event_type, event.work_version) for event in events] == [("artifact_registered", 1)]
    with pytest.raises(WorkConflictError, match="different values"):
        repository.put_artifact_record(replace(artifact, filename="changed.docx"))
    with pytest.raises(WorkNotFoundError):
        repository.get_artifact_record(tenant_id="tenant_other", artifact_id=artifact.artifact_id)


def test_publication_record_is_atomic_idempotent_and_advances_work_status(
    repository: SQLAlchemyWorkRepository,
) -> None:
    repository.create_work(make_item())
    artifact = repository.put_artifact_record(make_artifact())
    publication = make_publication(artifact)

    assert repository.put_publication_record(publication) == publication
    assert repository.put_publication_record(publication) == publication
    assert repository.get_publication_record(
        tenant_id=publication.tenant_id,
        publication_id=publication.publication_id,
    ) == publication
    assert repository.list_publication_records(
        tenant_id=publication.tenant_id,
        work_id=publication.work_id,
    ) == (publication,)
    item = repository.get_work(tenant_id=publication.tenant_id, work_id=publication.work_id)
    assert item.status is WorkStatus.PUBLISHED
    assert item.version == 2
    events = repository.list_events(tenant_id=publication.tenant_id, work_id=publication.work_id)
    assert [(event.event_type, event.work_version) for event in events] == [
        ("artifact_registered", 1),
        ("publication_registered", 2),
    ]

    with pytest.raises(WorkConflictError, match="different values"):
        repository.put_publication_record(replace(publication, policy_id="changed-policy"))
    with pytest.raises(WorkNotFoundError):
        repository.get_publication_record(
            tenant_id="tenant_other",
            publication_id=publication.publication_id,
        )


def test_publication_binding_must_match_registered_artifact(
    repository: SQLAlchemyWorkRepository,
) -> None:
    repository.create_work(make_item())
    artifact = repository.put_artifact_record(make_artifact())

    with pytest.raises(WorkConflictError, match="binding does not match"):
        repository.put_publication_record(
            replace(make_publication(artifact), content_sha256=f"sha256:{'f' * 64}")
        )


def test_delivery_record_is_atomic_idempotent_and_grant_is_one_time(
    repository: SQLAlchemyWorkRepository,
) -> None:
    repository.create_work(make_item())
    artifact = repository.put_artifact_record(make_artifact())
    publication = repository.put_publication_record(make_publication(artifact))
    delivery = make_delivery(publication)

    assert repository.put_delivery_record(delivery) == delivery
    assert repository.put_delivery_record(delivery) == delivery
    assert repository.list_delivery_records(
        tenant_id=delivery.tenant_id,
        work_id=delivery.work_id,
    ) == (delivery,)
    item = repository.get_work(tenant_id=delivery.tenant_id, work_id=delivery.work_id)
    assert item.status is WorkStatus.DELIVERED
    assert [event.event_type for event in repository.list_events(
        tenant_id=delivery.tenant_id,
        work_id=delivery.work_id,
    )] == ["artifact_registered", "publication_registered", "delivery_registered"]

    consumed_at = delivery.delivered_at + timedelta(minutes=1)
    consumed = repository.redeem_delivery_grant(
        delivery_id=delivery.delivery_id,
        grant_hash=delivery.grant_hash,
        consumed_at=consumed_at,
    )
    assert consumed.grant_consumed_at == consumed_at
    with pytest.raises(DeliveryGrantConsumedError):
        repository.redeem_delivery_grant(
            delivery_id=delivery.delivery_id,
            grant_hash=delivery.grant_hash,
            consumed_at=consumed_at + timedelta(seconds=1),
        )
    with pytest.raises(DeliveryGrantNotFoundError):
        repository.redeem_delivery_grant(
            delivery_id=delivery.delivery_id,
            grant_hash=f"sha256:{'8' * 64}",
            consumed_at=consumed_at,
        )


def test_expired_delivery_grant_fails_closed(repository: SQLAlchemyWorkRepository) -> None:
    repository.create_work(make_item())
    artifact = repository.put_artifact_record(make_artifact())
    publication = repository.put_publication_record(make_publication(artifact))
    delivery = repository.put_delivery_record(make_delivery(publication))

    with pytest.raises(DeliveryGrantExpiredError):
        repository.redeem_delivery_grant(
            delivery_id=delivery.delivery_id,
            grant_hash=delivery.grant_hash,
            consumed_at=delivery.grant_expires_at,
        )


def test_later_artifact_creates_next_publication_while_work_remains_published(
    repository: SQLAlchemyWorkRepository,
) -> None:
    repository.create_work(make_item())
    first_artifact = repository.put_artifact_record(make_artifact())
    first = make_publication(first_artifact)
    repository.put_publication_record(first)
    second_artifact = repository.put_artifact_record(replace(
        first_artifact,
        artifact_id=f"artifact_{'f' * 64}",
        content_sha256=f"sha256:{'1' * 64}",
        storage_key=f"tenant_001/work_001/docx/{'1' * 64}.docx",
        filename="industry-report-draft-v2.docx",
        source_contract_version=2,
        source_digest=f"sha256:{'2' * 64}",
        approval_contract_version=2,
        approval_digest=f"sha256:{'3' * 64}",
        created_at=NOW + timedelta(seconds=3),
    ))
    second = replace(
        make_publication(
            second_artifact,
            version=2,
            published_at=NOW + timedelta(seconds=4),
        ),
        publication_id=f"publication_{'4' * 64}",
    )

    assert repository.put_publication_record(second) == second
    assert repository.put_publication_record(second) == second
    item = repository.get_work(tenant_id=second.tenant_id, work_id=second.work_id)
    assert item.status is WorkStatus.PUBLISHED
    assert item.version == 4
    assert repository.list_publication_records(
        tenant_id=second.tenant_id,
        work_id=second.work_id,
    ) == (first, second)
    assert [event.event_type for event in repository.list_events(
        tenant_id=second.tenant_id,
        work_id=second.work_id,
    )] == [
        "artifact_registered",
        "publication_registered",
        "artifact_registered",
        "publication_registered",
    ]


def test_different_request_is_rejected_when_same_playbook_scope_is_active(
    repository: SQLAlchemyWorkRepository,
) -> None:
    original = repository.create_work(make_item()).item

    with pytest.raises(ActiveWorkConflictError) as raised:
        repository.create_work(make_item(work_id="work_002", idempotency_key="request_002"))

    assert raised.value.existing.work_id == original.work_id


def test_database_race_is_converted_to_typed_active_work_conflict(
    repository: SQLAlchemyWorkRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = repository.create_work(make_item()).item
    real_find = repository_module._find_active_work
    calls = 0

    def hide_active_scope_once(
        connection: Connection,
        *,
        tenant_id: str,
        requester_id: str,
        digital_employee_id: str,
        playbook_id: str,
    ) -> WorkItem | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return real_find(
            connection,
            tenant_id=tenant_id,
            requester_id=requester_id,
            digital_employee_id=digital_employee_id,
            playbook_id=playbook_id,
        )

    monkeypatch.setattr(repository_module, "_find_active_work", hide_active_scope_once)

    with pytest.raises(ActiveWorkConflictError) as raised:
        repository.create_work(make_item(work_id="work_racing", idempotency_key="request_racing"))

    assert calls == 2
    assert raised.value.existing.work_id == original.work_id


def test_different_playbooks_can_be_active_together(repository: SQLAlchemyWorkRepository) -> None:
    first = repository.create_work(make_item()).item
    second = repository.create_work(
        make_item(
            work_id="work_002",
            idempotency_key="request_002",
            playbook_id="another_report",
        )
    ).item

    assert first.playbook_id != second.playbook_id
    assert repository.find_active_work(
        tenant_id=second.tenant_id,
        requester_id=second.requester_id,
        digital_employee_id=second.digital_employee_id,
        playbook_id=second.playbook_id,
    ) == second


def test_terminal_work_releases_playbook_scope(repository: SQLAlchemyWorkRepository) -> None:
    original = repository.create_work(make_item()).item
    cancelled = transition(original, WorkStatus.CANCELLED, event_id="event_cancelled")
    repository.commit_transition(tenant_id=original.tenant_id, expected_version=0, result=cancelled)

    replacement = repository.create_work(make_item(work_id="work_002", idempotency_key="request_002"))

    assert replacement.created is True
    assert replacement.item.work_id == "work_002"


def test_find_current_work_is_tenant_requester_and_employee_scoped(
    repository: SQLAlchemyWorkRepository,
) -> None:
    first = repository.create_work(make_item(work_id="work_first", idempotency_key="request_first")).item
    later = repository.create_work(
        make_item(
            work_id="work_later",
            idempotency_key="request_later",
            playbook_id="another_report",
        )
    ).item

    current = repository.find_current_work(
        tenant_id=later.tenant_id,
        requester_id=later.requester_id,
        digital_employee_id=later.digital_employee_id,
    )

    assert current is not None
    assert current.work_id == later.work_id
    assert (
        repository.find_current_work(
            tenant_id="tenant_other",
            requester_id=first.requester_id,
            digital_employee_id=first.digital_employee_id,
        )
        is None
    )


def test_create_requires_registered_matching_pack_snapshot(
    repository: SQLAlchemyWorkRepository,
) -> None:
    with pytest.raises(WorkConflictError, match="unregistered pack snapshot"):
        repository.create_work(replace(make_item(), pack_snapshot_id="missing"))
    with pytest.raises(WorkConflictError, match="does not match"):
        repository.create_work(replace(make_item(), pack_id="other-pack"))


def test_tenant_scope_hides_other_tenant_work(repository: SQLAlchemyWorkRepository) -> None:
    repository.create_work(make_item())

    with pytest.raises(WorkNotFoundError):
        repository.get_work(tenant_id="tenant_other", work_id="work_001")
    with pytest.raises(WorkNotFoundError):
        repository.list_events(tenant_id="tenant_other", work_id="work_001")


def test_work_contract_create_confirm_and_revise_round_trip(
    repository: SQLAlchemyWorkRepository,
) -> None:
    repository.create_work(make_item())
    first = make_contract()

    assert repository.create_work_contract(first) == first
    assert repository.create_work_contract(first) == first
    confirmed = repository.confirm_work_contract(
        tenant_id=first.tenant_id,
        work_id=first.work_id,
        contract_type=first.contract_type,
        expected_contract_version=1,
        confirmed_by=first.created_by,
        confirmed_at=NOW + timedelta(minutes=1),
    )
    revised = make_contract(
        version=2,
        title="2026年中国证券行业发展研究报告",
        created_at=NOW + timedelta(minutes=2),
    )

    assert confirmed.status is WorkContractStatus.CONFIRMED
    assert repository.confirm_work_contract(
        tenant_id=first.tenant_id,
        work_id=first.work_id,
        contract_type=first.contract_type,
        expected_contract_version=1,
        confirmed_by=first.created_by,
        confirmed_at=NOW + timedelta(minutes=3),
    ) == confirmed
    assert repository.revise_work_contract(revised) == revised
    assert repository.get_current_work_contract(
        tenant_id=first.tenant_id,
        work_id=first.work_id,
        contract_type=first.contract_type,
    ) == revised
    history = repository.list_work_contracts(
        tenant_id=first.tenant_id,
        work_id=first.work_id,
        contract_type=first.contract_type,
    )
    assert [entry.status for entry in history] == [
        WorkContractStatus.SUPERSEDED,
        WorkContractStatus.PROVISIONAL,
    ]
    assert history[0].confirmed_by == first.created_by


def test_work_contract_confirmation_is_requester_scoped_and_version_checked(
    repository: SQLAlchemyWorkRepository,
) -> None:
    repository.create_work(make_item())
    repository.create_work_contract(make_contract())

    with pytest.raises(WorkContractConflictError, match="only the WorkItem requester"):
        repository.confirm_work_contract(
            tenant_id="tenant_001",
            work_id="work_001",
            contract_type="report-brief",
            expected_contract_version=1,
            confirmed_by="employee_other",
            confirmed_at=NOW + timedelta(minutes=1),
        )
    with pytest.raises(WorkContractConflictError, match="version mismatch"):
        repository.confirm_work_contract(
            tenant_id="tenant_001",
            work_id="work_001",
            contract_type="report-brief",
            expected_contract_version=2,
            confirmed_by="employee_001",
            confirmed_at=NOW + timedelta(minutes=1),
        )


def test_work_contract_failed_revision_rolls_back_current_version(
    repository: SQLAlchemyWorkRepository,
) -> None:
    repository.create_work(make_item())
    first = make_contract()
    repository.create_work_contract(first)

    with pytest.raises(WorkContractConflictError, match="next version"):
        repository.revise_work_contract(
            make_contract(
                version=3,
                title="skipped version",
                created_at=NOW + timedelta(minutes=1),
            )
        )

    assert repository.get_current_work_contract(
        tenant_id=first.tenant_id,
        work_id=first.work_id,
        contract_type=first.contract_type,
    ) == first
    assert repository.list_work_contracts(
        tenant_id=first.tenant_id,
        work_id=first.work_id,
        contract_type=first.contract_type,
    ) == (first,)


def test_work_contract_rejects_cross_tenant_and_non_requester_binding(
    repository: SQLAlchemyWorkRepository,
) -> None:
    repository.create_work(make_item())

    with pytest.raises(WorkNotFoundError):
        repository.create_work_contract(replace(make_contract(), tenant_id="tenant_other"))
    with pytest.raises(WorkContractConflictError, match="creator must be the requester"):
        repository.create_work_contract(replace(make_contract(), created_by="employee_other"))


def test_brief_must_round_trip_through_json(repository: SQLAlchemyWorkRepository) -> None:
    item = replace(make_item(), brief={"bad": object()})

    with pytest.raises(NonJsonValueError, match="brief must be JSON-compatible"):
        repository.create_work(item)


def test_transition_updates_item_and_appends_event_atomically(
    repository: SQLAlchemyWorkRepository,
) -> None:
    draft = make_item()
    repository.create_work(draft)
    result = transition(draft, WorkStatus.QUEUED, event_id="event_001")

    stored = repository.commit_transition(
        tenant_id=draft.tenant_id,
        expected_version=0,
        result=result,
    )

    assert stored.status is WorkStatus.QUEUED
    assert repository.get_work(tenant_id=draft.tenant_id, work_id=draft.work_id).version == 1
    events = repository.list_events(tenant_id=draft.tenant_id, work_id=draft.work_id)
    assert len(events) == 1
    assert events[0].event_id == "event_001"
    assert events[0].work_version == 1


def test_stale_repository_update_fails_closed(repository: SQLAlchemyWorkRepository) -> None:
    draft = make_item()
    repository.create_work(draft)
    result = transition(draft, WorkStatus.QUEUED, event_id="event_001")

    with pytest.raises(OptimisticConcurrencyError):
        repository.commit_transition(
            tenant_id=draft.tenant_id,
            expected_version=7,
            result=result,
        )

    stored = repository.get_work(tenant_id=draft.tenant_id, work_id=draft.work_id)
    assert stored.status is WorkStatus.DRAFT
    assert stored.version == 0
    assert repository.list_events(tenant_id=draft.tenant_id, work_id=draft.work_id) == ()


def test_event_constraint_failure_rolls_back_item_update(
    repository: SQLAlchemyWorkRepository,
) -> None:
    draft = make_item()
    repository.create_work(draft)
    queued_result = transition(draft, WorkStatus.QUEUED, event_id="event_duplicate")
    queued = repository.commit_transition(
        tenant_id=draft.tenant_id,
        expected_version=0,
        result=queued_result,
    )
    running_result = transition(queued, WorkStatus.RUNNING, event_id="event_duplicate")

    with pytest.raises(WorkConflictError):
        repository.commit_transition(
            tenant_id=draft.tenant_id,
            expected_version=1,
            result=running_result,
        )

    stored = repository.get_work(tenant_id=draft.tenant_id, work_id=draft.work_id)
    assert stored.status is WorkStatus.QUEUED
    assert stored.version == 1
    assert len(repository.list_events(tenant_id=draft.tenant_id, work_id=draft.work_id)) == 1


def test_budget_replay_must_match_existing_values(repository: SQLAlchemyWorkRepository) -> None:
    repository.put_budget("budget_001", make_budget())
    changed = replace(make_budget(), max_model_calls=21)

    with pytest.raises(WorkConflictError, match="different values"):
        repository.put_budget("budget_001", changed)


def test_pack_snapshot_round_trip_is_idempotent_and_immutable(
    repository: SQLAlchemyWorkRepository,
) -> None:
    snapshot = PackSnapshot(
        pack_snapshot_id="pack_snapshot_sha256_content",
        pack_id="industry-report",
        pack_version="2.0.0",
        source_repository="https://example.invalid/repository",
        source_commit="commit_001",
        manifest_digest="sha256:manifest",
        content_artifact_id="pack-content://sha256/content",
        asset_version_refs=("strategic-report-docx@1.0.0:trusted-asset://template#digest",),
        created_at=NOW,
    )

    assert repository.put_pack_snapshot(snapshot) == snapshot
    assert repository.put_pack_snapshot(snapshot) == snapshot
    assert repository.get_pack_snapshot(pack_snapshot_id=snapshot.pack_snapshot_id) == snapshot
    with pytest.raises(WorkConflictError, match="different values"):
        repository.put_pack_snapshot(replace(snapshot, source_commit="commit_changed"))
