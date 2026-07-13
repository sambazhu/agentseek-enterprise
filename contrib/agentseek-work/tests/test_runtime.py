from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from agentseek_work.migrations import apply_migrations
from agentseek_work.models import ActorType, PackSnapshot, WorkBudget, WorkItem, WorkStatus
from agentseek_work.repository import SQLAlchemyWorkRepository, WorkConflictError
from agentseek_work.runtime import WorkRuntimeService
from agentseek_work.state_machine import TransitionResult, transition_work_item
from sqlalchemy import create_engine

NOW = datetime(2026, 7, 13, tzinfo=UTC)
LEASE = timedelta(seconds=30)


def make_item(*, work_id: str = "work_001", priority: int = 0) -> WorkItem:
    return WorkItem(
        work_id=work_id,
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
        idempotency_key=f"request:{work_id}",
        created_at=NOW,
        updated_at=NOW,
        priority=priority,
    )


@pytest.fixture
def repository() -> SQLAlchemyWorkRepository:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    apply_migrations(engine)
    repository = SQLAlchemyWorkRepository(engine)
    repository.put_budget(
        "budget_001",
        WorkBudget(
            max_model_calls=20,
            max_input_tokens=100_000,
            max_output_tokens=30_000,
            max_external_queries=50,
            max_phase_duration_seconds=600,
            max_work_duration_seconds=3000,
            max_retry_count=2,
        ),
    )
    repository.put_pack_snapshot(
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
    return repository


def queue(repository: SQLAlchemyWorkRepository, item: WorkItem, *, second: int = 1) -> WorkItem:
    repository.create_work(item)
    result = transition_work_item(
        item,
        to_status=WorkStatus.QUEUED,
        expected_version=0,
        event_id=f"event_queue_{item.work_id}",
        event_type="brief_confirmed",
        actor_type=ActorType.REQUESTER,
        actor_id="employee_001",
        occurred_at=NOW + timedelta(seconds=second),
        payload_digest="sha256:brief",
        policy_decision="allowed",
    )
    return repository.commit_transition(tenant_id=item.tenant_id, expected_version=0, result=result)


def runtime(repository: SQLAlchemyWorkRepository, worker_id: str = "worker_a") -> WorkRuntimeService:
    return WorkRuntimeService(repository=repository, worker_id=worker_id, lease_duration=LEASE)


def test_claim_prefers_priority_and_holds_finite_lease(repository: SQLAlchemyWorkRepository) -> None:
    queue(repository, make_item(work_id="low", priority=1))
    queue(repository, make_item(work_id="high", priority=5))

    claimed = runtime(repository).claim_next(now=NOW + timedelta(seconds=2))

    assert claimed is not None
    assert claimed.work_id == "high"
    assert claimed.status is WorkStatus.RUNNING
    assert claimed.phase_attempt == 1
    assert claimed.lease_owner == "worker_a"
    assert claimed.lease_expires_at == NOW + timedelta(seconds=32)


def test_claimed_work_is_not_claimed_twice(repository: SQLAlchemyWorkRepository) -> None:
    queue(repository, make_item())
    first = runtime(repository, "worker_a").claim_next(now=NOW + timedelta(seconds=2))
    second = runtime(repository, "worker_b").claim_next(now=NOW + timedelta(seconds=2))

    assert first is not None
    assert second is None


def test_renew_requires_current_unexpired_owner(repository: SQLAlchemyWorkRepository) -> None:
    queue(repository, make_item())
    claimed = runtime(repository, "worker_a").claim_next(now=NOW + timedelta(seconds=2))
    assert claimed is not None

    renewed = runtime(repository, "worker_a").renew(claimed, now=NOW + timedelta(seconds=10))
    assert renewed.lease_expires_at == NOW + timedelta(seconds=40)
    with pytest.raises(WorkConflictError):
        runtime(repository, "worker_b").renew(claimed, now=NOW + timedelta(seconds=11))


def test_waiting_external_commit_releases_slot_and_due_poll_requeues(
    repository: SQLAlchemyWorkRepository,
) -> None:
    queued = queue(repository, make_item())
    claimed = runtime(repository).claim_next(now=NOW + timedelta(seconds=2))
    assert claimed is not None
    result = transition_work_item(
        claimed,
        to_status=WorkStatus.WAITING_EXTERNAL,
        expected_version=claimed.version,
        event_id="event_waiting",
        event_type="external_task_submitted",
        actor_type=ActorType.DIGITAL_EMPLOYEE,
        actor_id="industry-report",
        occurred_at=NOW + timedelta(seconds=3),
        payload_digest="sha256:external",
        policy_decision="allowed",
    )
    due_at = NOW + timedelta(seconds=20)
    result = TransitionResult(
        item=replace(result.item, external_task_id="mineru_001", next_poll_at=due_at),
        event=result.event,
    )

    waiting = runtime(repository).commit(result, expected_version=claimed.version, now=NOW + timedelta(seconds=3))
    assert waiting.lease_owner is None
    assert waiting.status is WorkStatus.WAITING_EXTERNAL
    assert runtime(repository, "worker_other").claim_next(now=NOW + timedelta(seconds=4)) is None
    assert repository.requeue_due_external(event_id="event_poll_early", now=due_at - timedelta(seconds=1)) is None

    requeued = repository.requeue_due_external(event_id="event_poll_due", now=due_at)
    assert requeued is not None
    assert requeued.status is WorkStatus.QUEUED
    assert requeued.next_poll_at is None
    assert requeued.external_task_id == "mineru_001"
    assert queued.phase_attempt == 0


def test_wrong_worker_cannot_commit_phase_output(repository: SQLAlchemyWorkRepository) -> None:
    queue(repository, make_item())
    claimed = runtime(repository, "worker_a").claim_next(now=NOW + timedelta(seconds=2))
    assert claimed is not None
    result = transition_work_item(
        claimed,
        to_status=WorkStatus.WAITING_REVIEW,
        expected_version=claimed.version,
        event_id="event_review",
        event_type="outline_submitted",
        actor_type=ActorType.DIGITAL_EMPLOYEE,
        actor_id="industry-report",
        occurred_at=NOW + timedelta(seconds=3),
        payload_digest="sha256:outline",
        policy_decision="allowed",
    )

    with pytest.raises(WorkConflictError, match="active worker lease"):
        runtime(repository, "worker_b").commit(
            result,
            expected_version=claimed.version,
            now=NOW + timedelta(seconds=3),
        )
    stored = repository.get_work(tenant_id=claimed.tenant_id, work_id=claimed.work_id)
    assert stored.status is WorkStatus.RUNNING


def test_abandoned_lease_is_recovered_as_new_attempt(repository: SQLAlchemyWorkRepository) -> None:
    queue(repository, make_item())
    claimed = runtime(repository, "worker_a").claim_next(now=NOW + timedelta(seconds=2))
    assert claimed is not None
    assert runtime(repository, "worker_a").abandon(claimed, now=NOW + timedelta(seconds=3))

    recovered = runtime(repository, "worker_b").recover_one(now=NOW + timedelta(seconds=4))
    assert recovered is not None
    assert recovered.status is WorkStatus.RUNNING
    assert recovered.lease_owner == "worker_b"
    assert recovered.phase_attempt == 2
    assert recovered.version == claimed.version + 1


def test_retry_exhaustion_fails_and_releases_lease(repository: SQLAlchemyWorkRepository) -> None:
    queue(repository, make_item())
    current = runtime(repository, "worker_1").claim_next(now=NOW + timedelta(seconds=2))
    assert current is not None
    for attempt, worker_id in ((1, "worker_2"), (2, "worker_3")):
        runtime(repository, current.lease_owner or "").abandon(
            current,
            now=NOW + timedelta(seconds=2 + attempt * 2 - 1),
        )
        current = runtime(repository, worker_id).recover_one(
            now=NOW + timedelta(seconds=2 + attempt * 2),
        )
        assert current is not None
    runtime(repository, current.lease_owner or "").abandon(current, now=NOW + timedelta(seconds=7))

    failed = runtime(repository, "worker_4").recover_one(now=NOW + timedelta(seconds=8))
    assert failed is not None
    assert failed.status is WorkStatus.FAILED
    assert failed.lease_owner is None
    assert failed.phase_attempt == 3


def test_requester_cancel_clears_active_lease(repository: SQLAlchemyWorkRepository) -> None:
    queue(repository, make_item())
    claimed = runtime(repository).claim_next(now=NOW + timedelta(seconds=2))
    assert claimed is not None

    cancelled = repository.cancel_work(
        tenant_id=claimed.tenant_id,
        work_id=claimed.work_id,
        event_id="event_cancelled",
        actor_type=ActorType.REQUESTER,
        actor_id="employee_001",
        now=NOW + timedelta(seconds=3),
    )
    assert cancelled.status is WorkStatus.CANCELLED
    assert cancelled.lease_owner is None
    assert cancelled.lease_expires_at is None
