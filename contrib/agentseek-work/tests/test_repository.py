from dataclasses import replace
from datetime import UTC, datetime, timedelta

import agentseek_work.repository as repository_module
import pytest
from agentseek_work.migrations import LATEST_SCHEMA_VERSION, apply_migrations
from agentseek_work.models import ActorType, PackSnapshot, WorkBudget, WorkItem, WorkStatus
from agentseek_work.repository import (
    ActiveWorkConflictError,
    NonJsonValueError,
    SQLAlchemyWorkRepository,
    WorkConflictError,
    WorkNotFoundError,
)
from agentseek_work.schema import schema_versions
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


def test_create_is_idempotent_within_tenant(repository: SQLAlchemyWorkRepository) -> None:
    original = make_item()
    first = repository.create_work(original)
    replay = repository.create_work(make_item(work_id="work_replayed", idempotency_key=original.idempotency_key))

    assert first.created
    assert not replay.created
    assert replay.item.work_id == original.work_id


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
