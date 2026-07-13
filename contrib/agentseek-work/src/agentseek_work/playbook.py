from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from agentseek_work.models import BudgetAmount, BudgetReservation, WorkItem, WorkStatus


class PlaybookRegistryError(RuntimeError):
    """Raised when a configured playbook cannot be resolved exactly."""


@dataclass(frozen=True, slots=True)
class PhasePlan:
    reservation: BudgetAmount
    reservation_key: str = "phase"

    def __post_init__(self) -> None:
        if self.reservation.is_zero:
            raise ValueError("phase reservation must not be zero")
        if not self.reservation_key.strip():
            raise ValueError("reservation_key must not be blank")


@dataclass(frozen=True, slots=True)
class PhaseExecutionContext:
    item: WorkItem
    reservation: BudgetReservation
    phase_started_at: datetime
    phase_deadline: datetime
    work_deadline: datetime
    shutdown_requested: Callable[[], bool]
    renew_lease: Callable[[datetime], WorkItem]


@dataclass(frozen=True, slots=True)
class PhaseOutcome:
    to_status: WorkStatus
    event_type: str
    payload_digest: str
    actual_usage: BudgetAmount
    policy_decision: str = "allowed"
    external_task_id: str | None = None
    next_poll_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.to_status not in {
            WorkStatus.WAITING_EXTERNAL,
            WorkStatus.WAITING_INPUT,
            WorkStatus.WAITING_REVIEW,
            WorkStatus.WAITING_APPROVAL,
            WorkStatus.SUCCEEDED,
            WorkStatus.FAILED,
        }:
            raise ValueError("a phase outcome must leave running")
        for value, field_name in (
            (self.event_type, "event_type"),
            (self.payload_digest, "payload_digest"),
            (self.policy_decision, "policy_decision"),
        ):
            if not value.strip():
                raise ValueError(f"{field_name} must not be blank")
        if self.to_status is WorkStatus.WAITING_EXTERNAL:
            if self.external_task_id is None or not self.external_task_id.strip():
                raise ValueError("waiting_external requires external_task_id")
            if self.next_poll_at is None:
                raise ValueError("waiting_external requires next_poll_at")
        if self.next_poll_at is not None and (
            self.next_poll_at.tzinfo is None or self.next_poll_at.utcoffset() is None
        ):
            raise ValueError("next_poll_at must be timezone-aware")


@runtime_checkable
class WorkPlaybook(Protocol):
    playbook_id: str
    version: str

    def validate_brief(self, item: WorkItem) -> None: ...

    def plan_phase(self, item: WorkItem) -> PhasePlan: ...

    def allowed_transitions(self, item: WorkItem) -> frozenset[WorkStatus]: ...

    def run_phase(self, context: PhaseExecutionContext) -> PhaseOutcome: ...

    def validate_output(self, item: WorkItem, outcome: PhaseOutcome) -> None: ...


class WorkPlaybookRegistry:
    def __init__(self) -> None:
        self._playbooks: dict[tuple[str, str], WorkPlaybook] = {}

    def register(self, playbook: WorkPlaybook) -> None:
        key = _playbook_key(playbook.playbook_id, playbook.version)
        if key in self._playbooks:
            raise PlaybookRegistryError(f"playbook {key[0]}/{key[1]} is already registered")
        self._playbooks[key] = playbook

    def resolve(self, playbook_id: str, version: str) -> WorkPlaybook:
        key = _playbook_key(playbook_id, version)
        try:
            return self._playbooks[key]
        except KeyError as exc:
            raise PlaybookRegistryError(f"playbook {key[0]}/{key[1]} is not registered") from exc


def _playbook_key(playbook_id: str, version: str) -> tuple[str, str]:
    key = (playbook_id.strip(), version.strip())
    if not all(key):
        raise ValueError("playbook_id and version must not be blank")
    return key
