from datetime import UTC, datetime, timedelta

import pytest
from agentseek_work.migrations import apply_migrations
from agentseek_work.models import ActorType, BudgetAmount, PackSnapshot, WorkBudget, WorkItem, WorkStatus
from agentseek_work.playbook import (
    PhaseExecutionContext,
    PhaseOutcome,
    PhasePlan,
    PlaybookRegistryError,
    WorkPlaybookRegistry,
)
from agentseek_work.repository import SQLAlchemyWorkRepository
from agentseek_work.state_machine import transition_work_item
from agentseek_work.worker import PhaseWorker, phase_payload_digest
from sqlalchemy import create_engine

NOW = datetime(2026, 7, 13, 9, tzinfo=UTC)


class FakePlaybook:
    playbook_id = "securities_industry_report"
    version = "1"

    def __init__(self, *, plan: PhasePlan, outcome: PhaseOutcome) -> None:
        self.plan = plan
        self.outcome = outcome
        self.run_count = 0
        self.last_context: PhaseExecutionContext | None = None

    def validate_brief(self, item: WorkItem) -> None:
        assert item.brief["title"]

    def plan_phase(self, item: WorkItem) -> PhasePlan:
        assert item.current_phase == "intake"
        return self.plan

    def allowed_transitions(self, item: WorkItem) -> frozenset[WorkStatus]:
        return frozenset({WorkStatus.SUCCEEDED, WorkStatus.WAITING_EXTERNAL})

    def run_phase(self, context: PhaseExecutionContext) -> PhaseOutcome:
        self.run_count += 1
        self.last_context = context
        return self.outcome

    def validate_output(self, item: WorkItem, outcome: PhaseOutcome) -> None:
        assert outcome.payload_digest.startswith("sha256:")


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


def queue(repository: SQLAlchemyWorkRepository) -> WorkItem:
    item = WorkItem(
        work_id="work_worker",
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
        idempotency_key="request:worker",
        created_at=NOW,
        updated_at=NOW,
        brief={"title": "2025年中国证券行业发展研究报告"},
    )
    repository.create_work(item)
    result = transition_work_item(
        item,
        to_status=WorkStatus.QUEUED,
        expected_version=0,
        event_id="event_queue_worker",
        event_type="brief_confirmed",
        actor_type=ActorType.REQUESTER,
        actor_id="employee_001",
        occurred_at=NOW + timedelta(seconds=1),
        payload_digest="sha256:brief",
        policy_decision="allowed",
    )
    return repository.commit_transition(tenant_id=item.tenant_id, expected_version=0, result=result)


def playbook(*, status: WorkStatus = WorkStatus.SUCCEEDED) -> FakePlaybook:
    return FakePlaybook(
        plan=PhasePlan(BudgetAmount(model_calls=1, input_tokens=400, output_tokens=200)),
        outcome=PhaseOutcome(
            to_status=status,
            event_type="phase_completed",
            payload_digest=phase_payload_digest(b"phase output"),
            actual_usage=BudgetAmount(model_calls=1, input_tokens=250, output_tokens=120),
            external_task_id="external_001" if status is WorkStatus.WAITING_EXTERNAL else None,
            next_poll_at=NOW + timedelta(minutes=3) if status is WorkStatus.WAITING_EXTERNAL else None,
        ),
    )


def worker(
    repository: SQLAlchemyWorkRepository,
    selected: FakePlaybook,
    *,
    completed_at: datetime | None = None,
) -> PhaseWorker:
    registry = WorkPlaybookRegistry()
    registry.register(selected)
    return PhaseWorker(
        repository=repository,
        registry=registry,
        worker_id="worker_a",
        lease_duration=timedelta(minutes=5),
        clock=lambda: completed_at or NOW + timedelta(seconds=3),
    )


def test_worker_claims_runs_settles_and_commits_one_phase(
    repository: SQLAlchemyWorkRepository,
) -> None:
    queued = queue(repository)
    selected = playbook()
    result = worker(repository, selected).run_once(now=NOW + timedelta(seconds=2))

    assert result is not None
    assert result.outcome == "phase_committed"
    assert result.item.status is WorkStatus.SUCCEEDED
    assert result.item.lease_owner is None
    assert selected.run_count == 1
    assert selected.last_context is not None
    assert selected.last_context.phase_deadline == NOW + timedelta(seconds=302)
    usage = repository.get_budget_usage(tenant_id=queued.tenant_id, work_id=queued.work_id)
    assert usage.used == selected.outcome.actual_usage
    assert usage.reserved.is_zero


def test_worker_commits_waiting_external_and_releases_slot(
    repository: SQLAlchemyWorkRepository,
) -> None:
    queue(repository)
    selected = playbook(status=WorkStatus.WAITING_EXTERNAL)
    result = worker(repository, selected).run_once(now=NOW + timedelta(seconds=2))

    assert result is not None
    assert result.item.status is WorkStatus.WAITING_EXTERNAL
    assert result.item.external_task_id == "external_001"
    assert result.item.next_poll_at == NOW + timedelta(minutes=3)
    assert result.item.lease_owner is None


def test_budget_exhaustion_waits_for_approval_without_calling_playbook(
    repository: SQLAlchemyWorkRepository,
) -> None:
    queue(repository)
    selected = FakePlaybook(
        plan=PhasePlan(BudgetAmount(model_calls=3)),
        outcome=playbook().outcome,
    )
    result = worker(repository, selected).run_once(now=NOW + timedelta(seconds=2))

    assert result is not None
    assert result.outcome == "budget_extension_required"
    assert result.item.status is WorkStatus.WAITING_APPROVAL
    assert result.item.approval_state == "budget_extension_required"
    assert selected.run_count == 0
    events = repository.list_events(tenant_id=result.item.tenant_id, work_id=result.item.work_id)
    assert events[-1].event_type == "budget_extension_required"
    assert events[-1].policy_decision == "budget_exhausted"


def test_missing_exact_playbook_abandons_lease_for_recovery(
    repository: SQLAlchemyWorkRepository,
) -> None:
    queued = queue(repository)
    runner = PhaseWorker(
        repository=repository,
        registry=WorkPlaybookRegistry(),
        worker_id="worker_a",
        lease_duration=timedelta(minutes=5),
        clock=lambda: NOW + timedelta(seconds=3),
    )
    with pytest.raises(PlaybookRegistryError, match="not registered"):
        runner.run_once(now=NOW + timedelta(seconds=2))
    stored = repository.get_work(tenant_id=queued.tenant_id, work_id=queued.work_id)
    assert stored.status is WorkStatus.RUNNING
    assert stored.lease_expires_at == NOW + timedelta(seconds=3)


def test_phase_timeout_keeps_reservation_for_conservative_recovery(
    repository: SQLAlchemyWorkRepository,
) -> None:
    queued = queue(repository)
    selected = playbook()
    runner = worker(repository, selected, completed_at=NOW + timedelta(seconds=303))
    with pytest.raises(TimeoutError, match="execution deadline"):
        runner.run_once(now=NOW + timedelta(seconds=2))
    usage = repository.get_budget_usage(tenant_id=queued.tenant_id, work_id=queued.work_id)
    assert usage.used.is_zero
    assert not usage.reserved.is_zero


def test_graceful_shutdown_stops_new_claims(repository: SQLAlchemyWorkRepository) -> None:
    queued = queue(repository)
    runner = worker(repository, playbook())
    runner.request_shutdown()
    assert runner.run_once(now=NOW + timedelta(seconds=2)) is None
    assert repository.get_work(tenant_id=queued.tenant_id, work_id=queued.work_id).status is WorkStatus.QUEUED


def test_playbook_registry_requires_exact_unique_version() -> None:
    selected = playbook()
    registry = WorkPlaybookRegistry()
    registry.register(selected)

    assert registry.resolve(selected.playbook_id, selected.version) is selected
    with pytest.raises(PlaybookRegistryError, match="already registered"):
        registry.register(selected)
    with pytest.raises(PlaybookRegistryError, match="not registered"):
        registry.resolve(selected.playbook_id, "2")
