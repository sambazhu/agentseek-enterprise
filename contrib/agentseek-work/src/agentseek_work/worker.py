from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid4, uuid5

from agentseek_work.models import ActorType, BudgetReservation, WorkItem, WorkStatus
from agentseek_work.playbook import (
    PhaseExecutionContext,
    PhaseOutcome,
    WorkPlaybookRegistry,
)
from agentseek_work.repository import BudgetExceededError, SQLAlchemyWorkRepository
from agentseek_work.runtime import WorkRuntimeService
from agentseek_work.state_machine import TransitionResult, transition_work_item


@dataclass(frozen=True, slots=True)
class PhaseRunResult:
    item: WorkItem
    outcome: str
    reservation: BudgetReservation | None = None


class PhaseWorker:
    """Runs at most one durable phase for each claimed WorkItem."""

    def __init__(
        self,
        *,
        repository: SQLAlchemyWorkRepository,
        registry: WorkPlaybookRegistry,
        worker_id: str,
        lease_duration: timedelta,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._registry = registry
        self._runtime = WorkRuntimeService(
            repository=repository,
            worker_id=worker_id,
            lease_duration=lease_duration,
        )
        self._worker_id = worker_id
        self._clock = clock or _utcnow
        self._shutdown_requested = False
        self._current_item: WorkItem | None = None

    @property
    def current_item(self) -> WorkItem | None:
        return self._current_item

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown_requested

    def request_shutdown(self) -> None:
        self._shutdown_requested = True

    def abandon_current(self, *, now: datetime | None = None) -> bool:
        self.request_shutdown()
        item = self._current_item
        if item is None:
            return False
        return self._runtime.abandon(item, now=now or self._clock())

    def run_once(
        self,
        *,
        now: datetime | None = None,
        tenant_id: str | None = None,
    ) -> PhaseRunResult | None:
        if self._shutdown_requested:
            return None
        started_at = now or self._clock()
        item = self._runtime.claim_next(now=started_at, tenant_id=tenant_id)
        if item is None:
            return None
        self._current_item = item
        reservation: BudgetReservation | None = None
        try:
            playbook = self._registry.resolve(item.playbook_id, item.playbook_version)
            playbook.validate_brief(item)
            plan = playbook.plan_phase(item)
            reservation_key = f"{item.work_id}:{item.current_phase}:{item.phase_attempt}:{plan.reservation_key}"
            try:
                reservation = self._repository.reserve_budget(
                    tenant_id=item.tenant_id,
                    work_id=item.work_id,
                    worker_id=self._worker_id,
                    reservation_id=f"reservation_{uuid5(NAMESPACE_URL, reservation_key).hex}",
                    idempotency_key=reservation_key,
                    amount=plan.reservation,
                    now=started_at,
                )
            except BudgetExceededError:
                waiting = _budget_extension_transition(item, now=started_at, worker_id=self._worker_id)
                committed = self._runtime.commit(
                    waiting,
                    expected_version=item.version,
                    now=started_at,
                )
                return PhaseRunResult(item=committed, outcome="budget_extension_required")

            if self._shutdown_requested:
                self._repository.release_budget(
                    reservation_id=reservation.reservation_id,
                    tenant_id=item.tenant_id,
                    worker_id=self._worker_id,
                    now=started_at,
                )
                self._runtime.abandon(item, now=started_at)
                return PhaseRunResult(item=item, outcome="shutdown_abandoned", reservation=reservation)

            budget = self._repository.get_budget_for_work(tenant_id=item.tenant_id, work_id=item.work_id)
            context = PhaseExecutionContext(
                item=item,
                reservation=reservation,
                phase_started_at=started_at,
                phase_deadline=started_at + timedelta(seconds=budget.max_phase_duration_seconds),
                work_deadline=item.created_at + timedelta(seconds=budget.max_work_duration_seconds),
                shutdown_requested=lambda: self._shutdown_requested,
                renew_lease=lambda heartbeat_at: self._runtime.renew(item, now=heartbeat_at),
            )
            outcome = playbook.run_phase(context)
            if outcome.to_status not in playbook.allowed_transitions(item):
                _raise_disallowed_playbook_transition(item, outcome)
            playbook.validate_output(item, outcome)
            completed_at = _at_or_after(self._clock(), started_at)
            if completed_at > min(context.phase_deadline, context.work_deadline):
                _raise_execution_timeout()
            self._repository.settle_budget(
                reservation_id=reservation.reservation_id,
                tenant_id=item.tenant_id,
                worker_id=self._worker_id,
                actual=outcome.actual_usage,
                now=completed_at,
            )
            result = _outcome_transition(item, outcome=outcome, now=completed_at, worker_id=self._worker_id)
            committed = self._runtime.commit(result, expected_version=item.version, now=completed_at)
            return PhaseRunResult(item=committed, outcome="phase_committed", reservation=reservation)
        except Exception:
            self._runtime.abandon(item, now=_at_or_after(self._clock(), started_at))
            raise
        finally:
            self._current_item = None


def _budget_extension_transition(item: WorkItem, *, now: datetime, worker_id: str) -> TransitionResult:
    result = transition_work_item(
        item,
        to_status=WorkStatus.WAITING_APPROVAL,
        expected_version=item.version,
        event_id=f"event_{uuid4().hex}",
        event_type="budget_extension_required",
        actor_type=ActorType.SYSTEM,
        actor_id=worker_id,
        occurred_at=now,
        payload_digest="sha256:budget_extension_required",
        policy_decision="budget_exhausted",
    )
    return TransitionResult(
        item=replace(result.item, approval_state="budget_extension_required"),
        event=result.event,
    )


def _outcome_transition(
    item: WorkItem,
    *,
    outcome: PhaseOutcome,
    now: datetime,
    worker_id: str,
) -> TransitionResult:
    result = transition_work_item(
        item,
        to_status=outcome.to_status,
        expected_version=item.version,
        event_id=f"event_{uuid4().hex}",
        event_type=outcome.event_type,
        actor_type=ActorType.DIGITAL_EMPLOYEE,
        actor_id=worker_id,
        occurred_at=now,
        payload_digest=outcome.payload_digest,
        policy_decision=outcome.policy_decision,
    )
    if outcome.to_status is not WorkStatus.WAITING_EXTERNAL:
        return result
    return TransitionResult(
        item=replace(
            result.item,
            external_task_id=outcome.external_task_id,
            next_poll_at=outcome.next_poll_at,
        ),
        event=result.event,
    )


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _at_or_after(current: datetime, minimum: datetime) -> datetime:
    return current if current >= minimum else minimum


def phase_payload_digest(payload: bytes) -> str:
    return f"sha256:{sha256(payload).hexdigest()}"


def _raise_disallowed_playbook_transition(item: WorkItem, outcome: PhaseOutcome) -> None:
    raise ValueError(f"playbook does not allow {item.current_phase} -> {outcome.to_status.value}")


def _raise_execution_timeout() -> None:
    raise TimeoutError("phase or work execution deadline was exceeded")
