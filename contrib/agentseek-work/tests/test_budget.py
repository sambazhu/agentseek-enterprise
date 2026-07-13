from datetime import UTC, datetime, timedelta

import pytest
from agentseek_work.migrations import apply_migrations
from agentseek_work.models import (
    ActorType,
    BudgetAmount,
    BudgetReservationStatus,
    PackSnapshot,
    WorkBudget,
    WorkItem,
    WorkStatus,
)
from agentseek_work.repository import (
    BudgetExceededError,
    BudgetReservationError,
    SQLAlchemyWorkRepository,
    WorkConflictError,
    WorkNotFoundError,
)
from agentseek_work.runtime import WorkRuntimeService
from agentseek_work.schema import work_items
from agentseek_work.state_machine import transition_work_item
from sqlalchemy import create_engine, update

NOW = datetime(2026, 7, 13, 8, tzinfo=UTC)
LEASE = timedelta(minutes=5)


def make_item() -> WorkItem:
    return WorkItem(
        work_id="work_budget",
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
        idempotency_key="request:budget",
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture
def repository() -> SQLAlchemyWorkRepository:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    apply_migrations(engine)
    repo = SQLAlchemyWorkRepository(engine)
    repo.put_budget(
        "budget_001",
        WorkBudget(
            max_model_calls=2,
            max_input_tokens=1_000,
            max_output_tokens=500,
            max_external_queries=3,
            max_phase_duration_seconds=300,
            max_work_duration_seconds=3_000,
            max_retry_count=2,
        ),
    )
    repo.put_pack_snapshot(
        PackSnapshot(
            pack_snapshot_id="sha256:pack",
            pack_id="industry-report",
            pack_version="1.0.0",
            manifest_digest="sha256:manifest",
            content_artifact_id="pack-content://sha256/content",
            asset_version_refs=(),
            created_at=NOW,
        )
    )
    return repo


def claim(repository: SQLAlchemyWorkRepository) -> tuple[WorkRuntimeService, WorkItem]:
    item = make_item()
    repository.create_work(item)
    queued_result = transition_work_item(
        item,
        to_status=WorkStatus.QUEUED,
        expected_version=0,
        event_id="event_queue_budget",
        event_type="brief_confirmed",
        actor_type=ActorType.REQUESTER,
        actor_id="employee_001",
        occurred_at=NOW + timedelta(seconds=1),
        payload_digest="sha256:brief",
        policy_decision="allowed",
    )
    repository.commit_transition(tenant_id=item.tenant_id, expected_version=0, result=queued_result)
    runtime = WorkRuntimeService(repository, "worker_a", LEASE)
    claimed = runtime.claim_next(now=NOW + timedelta(seconds=2))
    assert claimed is not None
    return runtime, claimed


def reserve(
    repository: SQLAlchemyWorkRepository,
    item: WorkItem,
    *,
    reservation_id: str = "reservation_001",
    idempotency_key: str = "intake:1:model",
    amount: BudgetAmount | None = None,
):
    return repository.reserve_budget(
        tenant_id=item.tenant_id,
        work_id=item.work_id,
        worker_id="worker_a",
        reservation_id=reservation_id,
        idempotency_key=idempotency_key,
        amount=amount or BudgetAmount(model_calls=1, input_tokens=400, output_tokens=200),
        now=NOW + timedelta(seconds=3),
    )


def test_reserve_and_settle_records_actual_usage(repository: SQLAlchemyWorkRepository) -> None:
    _, item = claim(repository)
    reservation = reserve(repository, item)

    during = repository.get_budget_usage(tenant_id=item.tenant_id, work_id=item.work_id)
    assert during.used == BudgetAmount()
    assert during.reserved == reservation.reserved

    settled = repository.settle_budget(
        reservation_id=reservation.reservation_id,
        tenant_id=item.tenant_id,
        worker_id="worker_a",
        actual=BudgetAmount(model_calls=1, input_tokens=250, output_tokens=120),
        now=NOW + timedelta(seconds=4),
    )
    usage = repository.get_budget_usage(tenant_id=item.tenant_id, work_id=item.work_id)
    assert settled.status is BudgetReservationStatus.SETTLED
    assert usage.used == settled.actual
    assert usage.reserved == BudgetAmount()


def test_reservation_is_idempotent_and_capacity_is_atomic(repository: SQLAlchemyWorkRepository) -> None:
    _, item = claim(repository)
    first = reserve(repository, item)
    replay = reserve(repository, item)
    assert replay == first

    with pytest.raises(BudgetExceededError, match="input_tokens"):
        reserve(
            repository,
            item,
            reservation_id="reservation_002",
            idempotency_key="intake:1:second",
            amount=BudgetAmount(model_calls=1, input_tokens=700, output_tokens=100),
        )
    assert repository.get_budget_usage(tenant_id=item.tenant_id, work_id=item.work_id).reserved == first.reserved


def test_reservation_idempotency_key_cannot_be_reused_with_new_ceiling(
    repository: SQLAlchemyWorkRepository,
) -> None:
    _, item = claim(repository)
    reserve(repository, item)
    with pytest.raises(BudgetReservationError, match="reused with different values"):
        reserve(
            repository,
            item,
            reservation_id="reservation_changed",
            amount=BudgetAmount(model_calls=1, input_tokens=300, output_tokens=100),
        )


def test_release_returns_reserved_capacity(repository: SQLAlchemyWorkRepository) -> None:
    _, item = claim(repository)
    reservation = reserve(repository, item)
    released = repository.release_budget(
        reservation_id=reservation.reservation_id,
        tenant_id=item.tenant_id,
        worker_id="worker_a",
        now=NOW + timedelta(seconds=4),
    )
    assert released.status is BudgetReservationStatus.RELEASED
    assert repository.get_budget_usage(tenant_id=item.tenant_id, work_id=item.work_id).reserved.is_zero


def test_actual_usage_cannot_exceed_reservation(repository: SQLAlchemyWorkRepository) -> None:
    _, item = claim(repository)
    reservation = reserve(repository, item)
    with pytest.raises(BudgetReservationError, match="exceeds"):
        repository.settle_budget(
            reservation_id=reservation.reservation_id,
            tenant_id=item.tenant_id,
            worker_id="worker_a",
            actual=BudgetAmount(model_calls=1, input_tokens=401, output_tokens=200),
            now=NOW + timedelta(seconds=4),
        )
    assert (
        repository.get_budget_reservation(
            tenant_id=item.tenant_id,
            reservation_id=reservation.reservation_id,
        ).status
        is BudgetReservationStatus.ACTIVE
    )


def test_active_reservation_blocks_phase_commit(repository: SQLAlchemyWorkRepository) -> None:
    runtime, item = claim(repository)
    reserve(repository, item)
    result = transition_work_item(
        item,
        to_status=WorkStatus.SUCCEEDED,
        expected_version=item.version,
        event_id="event_done",
        event_type="phase_completed",
        actor_type=ActorType.DIGITAL_EMPLOYEE,
        actor_id="worker_a",
        occurred_at=NOW + timedelta(seconds=4),
        payload_digest="sha256:done",
        policy_decision="allowed",
    )
    with pytest.raises(BudgetReservationError, match="must be finalized"):
        runtime.commit(result, expected_version=item.version, now=NOW + timedelta(seconds=4))
    assert repository.get_work(tenant_id=item.tenant_id, work_id=item.work_id).status is WorkStatus.RUNNING


def test_expired_attempt_forfeits_unknown_usage_before_recovery(
    repository: SQLAlchemyWorkRepository,
) -> None:
    runtime, item = claim(repository)
    reservation = reserve(repository, item)
    runtime.abandon(item, now=NOW + timedelta(seconds=4))
    recovered = WorkRuntimeService(repository, "worker_b", LEASE).recover_one(now=NOW + timedelta(seconds=5))
    assert recovered is not None
    stored = repository.get_budget_reservation(
        tenant_id=item.tenant_id,
        reservation_id=reservation.reservation_id,
    )
    usage = repository.get_budget_usage(tenant_id=item.tenant_id, work_id=item.work_id)
    assert stored.status is BudgetReservationStatus.FORFEITED
    assert stored.actual == stored.reserved
    assert usage.used == stored.reserved
    assert usage.reserved.is_zero


def test_budget_access_is_tenant_and_lease_scoped(repository: SQLAlchemyWorkRepository) -> None:
    _, item = claim(repository)
    with pytest.raises(WorkNotFoundError):
        repository.get_budget_usage(tenant_id="tenant_other", work_id=item.work_id)
    with pytest.raises(WorkConflictError, match="active worker lease"):
        repository.reserve_budget(
            tenant_id=item.tenant_id,
            work_id=item.work_id,
            worker_id="worker_other",
            reservation_id="reservation_bad",
            idempotency_key="bad",
            amount=BudgetAmount(model_calls=1),
            now=NOW + timedelta(seconds=3),
        )


def test_work_duration_is_checked_before_new_reservation(repository: SQLAlchemyWorkRepository) -> None:
    _, item = claim(repository)
    with repository.engine.begin() as connection:
        connection.execute(
            update(work_items)
            .where(work_items.c.work_id == item.work_id)
            .values(lease_expires_at=NOW + timedelta(hours=2))
        )
    with pytest.raises(BudgetExceededError, match="max_work_duration"):
        repository.reserve_budget(
            tenant_id=item.tenant_id,
            work_id=item.work_id,
            worker_id="worker_a",
            reservation_id="reservation_late",
            idempotency_key="late",
            amount=BudgetAmount(model_calls=1),
            now=NOW + timedelta(seconds=3_001),
        )
