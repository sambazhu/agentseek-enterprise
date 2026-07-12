from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final

from agentseek_work.models import ActorType, WorkEvent, WorkItem, WorkStatus


class WorkStateError(RuntimeError):
    """Base class for deterministic work-state failures."""


class InvalidTransitionError(WorkStateError):
    """Raised when a status edge is not part of the frozen state machine."""


class OptimisticConcurrencyError(WorkStateError):
    """Raised when the caller is transitioning a stale WorkItem version."""


ALLOWED_TRANSITIONS: Final[dict[WorkStatus, frozenset[WorkStatus]]] = {
    WorkStatus.DRAFT: frozenset({WorkStatus.QUEUED, WorkStatus.CANCELLED}),
    WorkStatus.QUEUED: frozenset({WorkStatus.RUNNING, WorkStatus.FAILED, WorkStatus.CANCELLED}),
    WorkStatus.RUNNING: frozenset({
        WorkStatus.WAITING_EXTERNAL,
        WorkStatus.WAITING_INPUT,
        WorkStatus.WAITING_REVIEW,
        WorkStatus.WAITING_APPROVAL,
        WorkStatus.SUCCEEDED,
        WorkStatus.FAILED,
        WorkStatus.CANCELLED,
    }),
    WorkStatus.WAITING_EXTERNAL: frozenset({WorkStatus.QUEUED, WorkStatus.FAILED, WorkStatus.CANCELLED}),
    WorkStatus.WAITING_INPUT: frozenset({WorkStatus.QUEUED, WorkStatus.CANCELLED}),
    WorkStatus.WAITING_REVIEW: frozenset({WorkStatus.QUEUED, WorkStatus.CANCELLED}),
    WorkStatus.WAITING_APPROVAL: frozenset({WorkStatus.QUEUED, WorkStatus.CANCELLED}),
    WorkStatus.SUCCEEDED: frozenset(),
    WorkStatus.FAILED: frozenset(),
    WorkStatus.CANCELLED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class TransitionResult:
    item: WorkItem
    event: WorkEvent


def transition_work_item(
    item: WorkItem,
    *,
    to_status: WorkStatus,
    expected_version: int,
    event_id: str,
    event_type: str,
    actor_type: ActorType,
    actor_id: str,
    occurred_at: datetime,
    payload_digest: str,
    policy_decision: str,
    phase: str | None = None,
) -> TransitionResult:
    if item.version != expected_version:
        raise OptimisticConcurrencyError(
            f"work item version mismatch: expected {expected_version}, actual {item.version}"
        )
    if to_status not in ALLOWED_TRANSITIONS[item.status]:
        raise InvalidTransitionError(f"transition {item.status.value} -> {to_status.value} is not allowed")
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        raise ValueError("occurred_at must be timezone-aware")
    if occurred_at < item.updated_at:
        raise ValueError("occurred_at must not be earlier than the current WorkItem updated_at")

    next_phase = phase.strip() if phase is not None else item.current_phase
    if not next_phase:
        raise ValueError("phase must not be blank")
    next_version = item.version + 1
    next_attempt = item.phase_attempt + 1 if to_status is WorkStatus.RUNNING else item.phase_attempt
    updated_item = replace(
        item,
        status=to_status,
        current_phase=next_phase,
        phase_attempt=next_attempt,
        version=next_version,
        updated_at=occurred_at,
    )
    event = WorkEvent(
        event_id=event_id,
        work_id=item.work_id,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        phase=next_phase,
        from_status=item.status,
        to_status=to_status,
        work_version=next_version,
        payload_digest=payload_digest,
        policy_decision=policy_decision,
        occurred_at=occurred_at,
    )
    return TransitionResult(item=updated_item, event=event)
