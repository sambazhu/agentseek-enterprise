from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

from agentseek_work.models import (
    ActorType,
    BudgetReservationStatus,
    ClaimReviewerStatus,
    ClaimType,
    ClaimVerificationStatus,
    ExcerptStatus,
    PublicationStatus,
    SnapshotStatus,
    SourceType,
    WorkContractStatus,
    WorkStatus,
)

metadata = MetaData()
json_document = JSON().with_variant(JSONB(), "postgresql")


def _sql_values(values: list[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


work_status_values = _sql_values([status.value for status in WorkStatus])
actor_type_values = _sql_values([actor_type.value for actor_type in ActorType])
budget_reservation_status_values = _sql_values([status.value for status in BudgetReservationStatus])
work_contract_status_values = _sql_values([status.value for status in WorkContractStatus])
source_type_values = _sql_values([source_type.value for source_type in SourceType])
snapshot_status_values = _sql_values([status.value for status in SnapshotStatus])
excerpt_status_values = _sql_values([status.value for status in ExcerptStatus])
claim_type_values = _sql_values([claim_type.value for claim_type in ClaimType])
claim_verification_status_values = _sql_values([status.value for status in ClaimVerificationStatus])
claim_reviewer_status_values = _sql_values([status.value for status in ClaimReviewerStatus])
publication_status_values = _sql_values([status.value for status in PublicationStatus])

schema_versions = Table(
    "enterprise_work_schema_versions",
    metadata,
    Column("version", Integer, primary_key=True),
    Column("applied_at", DateTime(timezone=True), nullable=False),
)

work_budgets = Table(
    "enterprise_work_budgets",
    metadata,
    Column("budget_id", String(128), primary_key=True),
    Column("max_model_calls", Integer, nullable=False),
    Column("max_input_tokens", BigInteger, nullable=False),
    Column("max_output_tokens", BigInteger, nullable=False),
    Column("max_external_queries", Integer, nullable=False),
    Column("max_phase_duration_seconds", Integer, nullable=False),
    Column("max_work_duration_seconds", Integer, nullable=False),
    Column("max_retry_count", Integer, nullable=False),
    CheckConstraint("max_model_calls > 0", name="ck_work_budget_model_calls"),
    CheckConstraint("max_input_tokens > 0", name="ck_work_budget_input_tokens"),
    CheckConstraint("max_output_tokens > 0", name="ck_work_budget_output_tokens"),
    CheckConstraint("max_external_queries > 0", name="ck_work_budget_external_queries"),
    CheckConstraint("max_phase_duration_seconds > 0", name="ck_work_budget_phase_duration"),
    CheckConstraint("max_work_duration_seconds > 0", name="ck_work_budget_work_duration"),
    CheckConstraint("max_retry_count >= 0", name="ck_work_budget_retry_count"),
    CheckConstraint(
        "max_phase_duration_seconds <= max_work_duration_seconds",
        name="ck_work_budget_duration_order",
    ),
)

work_items = Table(
    "enterprise_work_items",
    metadata,
    Column("work_id", String(128), primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("digital_employee_id", String(128), nullable=False),
    Column("digital_employee_profile_version", String(64)),
    Column("digital_employee_permissions_digest", String(160)),
    Column("pack_id", String(128), nullable=False),
    Column("pack_version", String(64), nullable=False),
    Column("pack_snapshot_id", String(160), nullable=False),
    Column("skill_set_version", String(64)),
    Column("skill_digests", json_document, nullable=False),
    Column("runtime_release", String(128), nullable=False),
    Column("requester_id", String(128), nullable=False),
    Column("reviewer_id", String(128), nullable=False),
    Column("approver_id", String(128), nullable=False),
    Column("data_owner_id", String(128), nullable=False),
    Column("beneficiary_id", String(128), nullable=False),
    Column("playbook_id", String(128), nullable=False),
    Column("playbook_version", String(64), nullable=False),
    Column("brief", json_document, nullable=False),
    Column("status", String(32), nullable=False),
    Column("current_phase", String(128), nullable=False),
    Column("phase_attempt", Integer, nullable=False),
    Column("version", Integer, nullable=False),
    Column("priority", Integer, nullable=False),
    Column("input_file_ids", json_document, nullable=False),
    Column("source_ids", json_document, nullable=False),
    Column("artifact_ids", json_document, nullable=False),
    Column("approval_state", String(32), nullable=False),
    Column("budget_id", String(128), ForeignKey("enterprise_work_budgets.budget_id"), nullable=False),
    Column("external_task_id", String(256)),
    Column("next_poll_at", DateTime(timezone=True)),
    Column("lease_owner", String(128)),
    Column("lease_expires_at", DateTime(timezone=True)),
    Column("due_at", DateTime(timezone=True)),
    Column("idempotency_key", String(256), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("tenant_id", "idempotency_key", name="uq_work_items_tenant_idempotency"),
    CheckConstraint(f"status IN ({work_status_values})", name="ck_work_items_status"),
    CheckConstraint("phase_attempt >= 0", name="ck_work_items_phase_attempt"),
    CheckConstraint("version >= 0", name="ck_work_items_version"),
)

Index("ix_work_items_tenant_status", work_items.c.tenant_id, work_items.c.status)
Index("ix_work_items_next_poll", work_items.c.next_poll_at)
Index("ix_work_items_lease_expiry", work_items.c.lease_expires_at)

work_contracts = Table(
    "enterprise_work_contracts",
    metadata,
    Column(
        "work_id",
        String(128),
        ForeignKey("enterprise_work_items.work_id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    Column("contract_type", String(128), primary_key=True),
    Column("contract_version", Integer, primary_key=True),
    Column("tenant_id", String(128), nullable=False),
    Column("status", String(32), nullable=False),
    Column("payload", json_document, nullable=False),
    Column("created_by", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("confirmed_by", String(128)),
    Column("confirmed_at", DateTime(timezone=True)),
    Column("superseded_at", DateTime(timezone=True)),
    CheckConstraint("contract_version > 0", name="ck_work_contract_version"),
    CheckConstraint(f"status IN ({work_contract_status_values})", name="ck_work_contract_status"),
    CheckConstraint(
        "(confirmed_by IS NULL AND confirmed_at IS NULL) OR "
        "(confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL)",
        name="ck_work_contract_confirmation_pair",
    ),
    CheckConstraint(
        "(status = 'provisional' AND confirmed_at IS NULL AND superseded_at IS NULL) OR "
        "(status = 'confirmed' AND confirmed_at IS NOT NULL AND superseded_at IS NULL) OR "
        "(status = 'superseded' AND superseded_at IS NOT NULL)",
        name="ck_work_contract_lifecycle",
    ),
)

Index("ix_work_contracts_tenant_work", work_contracts.c.tenant_id, work_contracts.c.work_id)

work_sources = Table(
    "enterprise_work_sources",
    metadata,
    Column("source_id", String(160), primary_key=True),
    Column(
        "work_id",
        String(128),
        ForeignKey("enterprise_work_items.work_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("tenant_id", String(128), nullable=False),
    Column("source_type", String(48), nullable=False),
    Column("title", String(1024), nullable=False),
    Column("publisher", String(512), nullable=False),
    Column("published_at", DateTime(timezone=True)),
    Column("retrieved_at", DateTime(timezone=True), nullable=False),
    Column("locator", String(2048)),
    Column("uri_digest", String(160), nullable=False),
    Column("file_id", String(256)),
    Column("confidentiality_level", String(32), nullable=False),
    Column("authority_level", String(64), nullable=False),
    Column("allowed_uses", json_document, nullable=False),
    Column("content_hash", String(160), nullable=False),
    Column("result_digest", String(160), nullable=False),
    Column("snapshot_policy", String(64), nullable=False),
    Column("snapshot_status", String(32), nullable=False),
    Column("snapshot_artifact_id", String(256)),
    Column("license_restriction", String(1024)),
    Column("retrieval_query_digest", String(160), nullable=False),
    Column("license_terms_ref", String(512)),
    Column("excerpt_status", String(32), nullable=False),
    Column("metadata", json_document, nullable=False),
    CheckConstraint(f"source_type IN ({source_type_values})", name="ck_work_source_type"),
    CheckConstraint(f"snapshot_status IN ({snapshot_status_values})", name="ck_work_source_snapshot_status"),
    CheckConstraint(f"excerpt_status IN ({excerpt_status_values})", name="ck_work_source_excerpt_status"),
)

Index("ix_work_sources_tenant_work", work_sources.c.tenant_id, work_sources.c.work_id)

work_evidence = Table(
    "enterprise_work_evidence",
    metadata,
    Column("evidence_id", String(160), primary_key=True),
    Column(
        "work_id",
        String(128),
        ForeignKey("enterprise_work_items.work_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("tenant_id", String(128), nullable=False),
    Column(
        "source_id",
        String(160),
        ForeignKey("enterprise_work_sources.source_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("locator", String(2048), nullable=False),
    Column("excerpt", Text),
    Column("structured_value", json_document),
    Column("unit", String(128)),
    Column("period", String(256)),
    Column("confidence", Float, nullable=False),
    Column("extraction_method", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("metadata", json_document, nullable=False),
    CheckConstraint("confidence >= 0.0 AND confidence <= 1.0", name="ck_work_evidence_confidence"),
    CheckConstraint(
        "excerpt IS NOT NULL OR structured_value IS NOT NULL",
        name="ck_work_evidence_payload",
    ),
)

Index("ix_work_evidence_tenant_work", work_evidence.c.tenant_id, work_evidence.c.work_id)
Index("ix_work_evidence_source", work_evidence.c.source_id)

work_claims = Table(
    "enterprise_work_claims",
    metadata,
    Column("claim_id", String(160), primary_key=True),
    Column(
        "work_id",
        String(128),
        ForeignKey("enterprise_work_items.work_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("tenant_id", String(128), nullable=False),
    Column("section_id", String(256), nullable=False),
    Column("statement", Text, nullable=False),
    Column("claim_type", String(32), nullable=False),
    Column("verification_status", String(32), nullable=False),
    Column("reviewer_status", String(32), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("metadata", json_document, nullable=False),
    CheckConstraint(f"claim_type IN ({claim_type_values})", name="ck_work_claim_type"),
    CheckConstraint(
        f"verification_status IN ({claim_verification_status_values})",
        name="ck_work_claim_verification_status",
    ),
    CheckConstraint(
        f"reviewer_status IN ({claim_reviewer_status_values})",
        name="ck_work_claim_reviewer_status",
    ),
)

Index("ix_work_claims_tenant_work", work_claims.c.tenant_id, work_claims.c.work_id)
Index("ix_work_claims_section", work_claims.c.work_id, work_claims.c.section_id)

work_claim_evidence = Table(
    "enterprise_work_claim_evidence",
    metadata,
    Column(
        "claim_id",
        String(160),
        ForeignKey("enterprise_work_claims.claim_id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    Column(
        "evidence_id",
        String(160),
        ForeignKey("enterprise_work_evidence.evidence_id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    Column("ordinal", Integer, nullable=False),
    UniqueConstraint("claim_id", "ordinal", name="uq_work_claim_evidence_ordinal"),
    CheckConstraint("ordinal >= 0", name="ck_work_claim_evidence_ordinal"),
)

Index("ix_work_claim_evidence_evidence", work_claim_evidence.c.evidence_id)

work_artifacts = Table(
    "enterprise_work_artifacts",
    metadata,
    Column("artifact_id", String(160), primary_key=True),
    Column(
        "work_id",
        String(128),
        ForeignKey("enterprise_work_items.work_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("tenant_id", String(128), nullable=False),
    Column("artifact_type", String(64), nullable=False),
    Column("artifact_format", String(32), nullable=False),
    Column("media_type", String(128), nullable=False),
    Column("content_sha256", String(71), nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("storage_key", String(1024), nullable=False),
    Column("filename", String(256), nullable=False),
    Column("source_contract_type", String(128), nullable=False),
    Column("source_contract_version", Integer, nullable=False),
    Column("source_digest", String(71), nullable=False),
    Column("approval_contract_version", Integer, nullable=False),
    Column("approval_digest", String(71), nullable=False),
    Column("template_ref", String(512), nullable=False),
    Column("template_digest", String(71), nullable=False),
    Column("created_by", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("metadata", json_document, nullable=False),
    UniqueConstraint(
        "work_id",
        "artifact_format",
        "source_digest",
        "approval_digest",
        "template_digest",
        name="uq_work_artifacts_render_binding",
    ),
    CheckConstraint("size_bytes > 0", name="ck_work_artifact_size"),
    CheckConstraint(
        "source_contract_version > 0 AND approval_contract_version > 0",
        name="ck_work_artifact_contract_versions",
    ),
)

Index("ix_work_artifacts_tenant_work", work_artifacts.c.tenant_id, work_artifacts.c.work_id)
Index("ix_work_artifacts_content", work_artifacts.c.content_sha256)

work_publications = Table(
    "enterprise_work_publications",
    metadata,
    Column("publication_id", String(160), primary_key=True),
    Column("publication_version", Integer, nullable=False),
    Column(
        "work_id",
        String(128),
        ForeignKey("enterprise_work_items.work_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("tenant_id", String(128), nullable=False),
    Column(
        "artifact_id",
        String(160),
        ForeignKey("enterprise_work_artifacts.artifact_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("content_sha256", String(71), nullable=False),
    Column("source_contract_version", Integer, nullable=False),
    Column("approval_contract_version", Integer, nullable=False),
    Column("template_digest", String(71), nullable=False),
    Column("policy_id", String(128), nullable=False),
    Column("status", String(32), nullable=False),
    Column("published_by", String(128), nullable=False),
    Column("published_at", DateTime(timezone=True), nullable=False),
    Column("metadata", json_document, nullable=False),
    UniqueConstraint("work_id", "publication_version", name="uq_work_publications_version"),
    UniqueConstraint("work_id", "artifact_id", name="uq_work_publications_artifact"),
    CheckConstraint("publication_version > 0", name="ck_work_publication_version"),
    CheckConstraint(
        "source_contract_version > 0 AND approval_contract_version > 0",
        name="ck_work_publication_contract_versions",
    ),
    CheckConstraint(f"status IN ({publication_status_values})", name="ck_work_publication_status"),
)

Index("ix_work_publications_tenant_work", work_publications.c.tenant_id, work_publications.c.work_id)
Index("ix_work_publications_content", work_publications.c.content_sha256)

work_budget_usage = Table(
    "enterprise_work_budget_usage",
    metadata,
    Column(
        "work_id",
        String(128),
        ForeignKey("enterprise_work_items.work_id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    Column("tenant_id", String(128), nullable=False),
    Column("used_model_calls", Integer, nullable=False, default=0),
    Column("used_input_tokens", BigInteger, nullable=False, default=0),
    Column("used_output_tokens", BigInteger, nullable=False, default=0),
    Column("used_external_queries", Integer, nullable=False, default=0),
    Column("reserved_model_calls", Integer, nullable=False, default=0),
    Column("reserved_input_tokens", BigInteger, nullable=False, default=0),
    Column("reserved_output_tokens", BigInteger, nullable=False, default=0),
    Column("reserved_external_queries", Integer, nullable=False, default=0),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "used_model_calls >= 0 AND used_input_tokens >= 0 AND used_output_tokens >= 0 AND used_external_queries >= 0",
        name="ck_work_budget_usage_used_nonnegative",
    ),
    CheckConstraint(
        "reserved_model_calls >= 0 AND reserved_input_tokens >= 0 "
        "AND reserved_output_tokens >= 0 AND reserved_external_queries >= 0",
        name="ck_work_budget_usage_reserved_nonnegative",
    ),
)

Index("ix_work_budget_usage_tenant", work_budget_usage.c.tenant_id)

work_budget_reservations = Table(
    "enterprise_work_budget_reservations",
    metadata,
    Column("reservation_id", String(128), primary_key=True),
    Column(
        "work_id",
        String(128),
        ForeignKey("enterprise_work_items.work_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("tenant_id", String(128), nullable=False),
    Column("worker_id", String(128), nullable=False),
    Column("phase", String(128), nullable=False),
    Column("phase_attempt", Integer, nullable=False),
    Column("idempotency_key", String(256), nullable=False),
    Column("status", String(32), nullable=False),
    Column("reserved_model_calls", Integer, nullable=False),
    Column("reserved_input_tokens", BigInteger, nullable=False),
    Column("reserved_output_tokens", BigInteger, nullable=False),
    Column("reserved_external_queries", Integer, nullable=False),
    Column("actual_model_calls", Integer, nullable=False, default=0),
    Column("actual_input_tokens", BigInteger, nullable=False, default=0),
    Column("actual_output_tokens", BigInteger, nullable=False, default=0),
    Column("actual_external_queries", Integer, nullable=False, default=0),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("finalized_at", DateTime(timezone=True)),
    UniqueConstraint("work_id", "idempotency_key", name="uq_work_budget_reservation_idempotency"),
    CheckConstraint(
        f"status IN ({budget_reservation_status_values})",
        name="ck_work_budget_reservation_status",
    ),
    CheckConstraint("phase_attempt > 0", name="ck_work_budget_reservation_attempt"),
    CheckConstraint(
        "reserved_model_calls >= 0 AND reserved_input_tokens >= 0 "
        "AND reserved_output_tokens >= 0 AND reserved_external_queries >= 0",
        name="ck_work_budget_reservation_reserved_nonnegative",
    ),
    CheckConstraint(
        "actual_model_calls >= 0 AND actual_input_tokens >= 0 "
        "AND actual_output_tokens >= 0 AND actual_external_queries >= 0",
        name="ck_work_budget_reservation_actual_nonnegative",
    ),
    CheckConstraint(
        "actual_model_calls <= reserved_model_calls AND actual_input_tokens <= reserved_input_tokens "
        "AND actual_output_tokens <= reserved_output_tokens "
        "AND actual_external_queries <= reserved_external_queries",
        name="ck_work_budget_reservation_actual_within_reserved",
    ),
    CheckConstraint(
        "(status = 'active' AND finalized_at IS NULL) OR (status <> 'active' AND finalized_at IS NOT NULL)",
        name="ck_work_budget_reservation_finalized",
    ),
)

Index(
    "ix_work_budget_reservations_work_status",
    work_budget_reservations.c.work_id,
    work_budget_reservations.c.status,
)

pack_snapshots = Table(
    "enterprise_pack_snapshots",
    metadata,
    Column("pack_snapshot_id", String(160), primary_key=True),
    Column("pack_id", String(128), nullable=False),
    Column("pack_version", String(64), nullable=False),
    Column("source_repository", String(512)),
    Column("source_commit", String(160)),
    Column("manifest_digest", String(160), nullable=False),
    Column("content_artifact_id", String(256), nullable=False),
    Column("asset_version_refs", json_document, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "pack_id",
        "pack_version",
        "manifest_digest",
        name="uq_pack_snapshots_version_digest",
    ),
)

Index("ix_pack_snapshots_pack_version", pack_snapshots.c.pack_id, pack_snapshots.c.pack_version)

work_events = Table(
    "enterprise_work_events",
    metadata,
    Column("event_id", String(128), primary_key=True),
    Column(
        "work_id",
        String(128),
        ForeignKey("enterprise_work_items.work_id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("event_type", String(128), nullable=False),
    Column("actor_type", String(32), nullable=False),
    Column("actor_id", String(128), nullable=False),
    Column("phase", String(128), nullable=False),
    Column("from_status", String(32), nullable=False),
    Column("to_status", String(32), nullable=False),
    Column("work_version", Integer, nullable=False),
    Column("payload_digest", String(160), nullable=False),
    Column("policy_decision", String(128), nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("work_id", "work_version", name="uq_work_events_work_version"),
    CheckConstraint(f"actor_type IN ({actor_type_values})", name="ck_work_events_actor_type"),
    CheckConstraint(f"from_status IN ({work_status_values})", name="ck_work_events_from_status"),
    CheckConstraint(f"to_status IN ({work_status_values})", name="ck_work_events_to_status"),
    CheckConstraint("work_version > 0", name="ck_work_events_version"),
)

Index("ix_work_events_work_occurred", work_events.c.work_id, work_events.c.occurred_at)
