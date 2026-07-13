from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from agentseek_work.migrations import LATEST_SCHEMA_VERSION, apply_migrations
from agentseek_work.models import ActorType, PackSnapshot, WorkBudget, WorkItem, WorkStatus
from agentseek_work.repository import (
    NonJsonValueError,
    SQLAlchemyWorkRepository,
    WorkConflictError,
    WorkNotFoundError,
)
from agentseek_work.schema import schema_versions
from agentseek_work.state_machine import OptimisticConcurrencyError, transition_work_item
from sqlalchemy import create_engine, insert

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
) -> WorkItem:
    return WorkItem(
        work_id=work_id,
        tenant_id=tenant_id,
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


def test_create_is_idempotent_within_tenant(repository: SQLAlchemyWorkRepository) -> None:
    original = make_item()
    first = repository.create_work(original)
    replay = repository.create_work(make_item(work_id="work_replayed", idempotency_key=original.idempotency_key))

    assert first.created
    assert not replay.created
    assert replay.item.work_id == original.work_id


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
