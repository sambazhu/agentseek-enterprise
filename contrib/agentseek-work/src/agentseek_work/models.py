from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class WorkStatus(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_EXTERNAL = "waiting_external"
    WAITING_INPUT = "waiting_input"
    WAITING_REVIEW = "waiting_review"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ActorType(StrEnum):
    REQUESTER = "requester"
    DIGITAL_EMPLOYEE = "digital_employee"
    OPERATOR = "operator"
    SYSTEM = "system"


class BudgetReservationStatus(StrEnum):
    ACTIVE = "active"
    SETTLED = "settled"
    RELEASED = "released"
    FORFEITED = "forfeited"


TERMINAL_WORK_STATUSES = frozenset({
    WorkStatus.SUCCEEDED,
    WorkStatus.FAILED,
    WorkStatus.CANCELLED,
})


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class WorkBudget:
    max_model_calls: int
    max_input_tokens: int
    max_output_tokens: int
    max_external_queries: int
    max_phase_duration_seconds: int
    max_work_duration_seconds: int
    max_retry_count: int

    def __post_init__(self) -> None:
        for field_name in (
            "max_model_calls",
            "max_input_tokens",
            "max_output_tokens",
            "max_external_queries",
            "max_phase_duration_seconds",
            "max_work_duration_seconds",
        ):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be greater than zero")
        if self.max_retry_count < 0:
            raise ValueError("max_retry_count must be non-negative")
        if self.max_phase_duration_seconds > self.max_work_duration_seconds:
            raise ValueError("max_phase_duration_seconds must not exceed max_work_duration_seconds")


@dataclass(frozen=True, slots=True)
class BudgetAmount:
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    external_queries: int = 0

    def __post_init__(self) -> None:
        for field_name in ("model_calls", "input_tokens", "output_tokens", "external_queries"):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} must be non-negative")

    @property
    def is_zero(self) -> bool:
        return not any((self.model_calls, self.input_tokens, self.output_tokens, self.external_queries))


@dataclass(frozen=True, slots=True)
class BudgetUsage:
    used: BudgetAmount = field(default_factory=BudgetAmount)
    reserved: BudgetAmount = field(default_factory=BudgetAmount)


@dataclass(frozen=True, slots=True)
class BudgetReservation:
    reservation_id: str
    work_id: str
    tenant_id: str
    worker_id: str
    phase: str
    phase_attempt: int
    idempotency_key: str
    status: BudgetReservationStatus
    reserved: BudgetAmount
    actual: BudgetAmount
    created_at: datetime
    finalized_at: datetime | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "reservation_id",
            "work_id",
            "tenant_id",
            "worker_id",
            "phase",
            "idempotency_key",
        ):
            _require_text(getattr(self, field_name), field_name)
        if self.phase_attempt <= 0:
            raise ValueError("phase_attempt must be greater than zero")
        if self.reserved.is_zero:
            raise ValueError("reserved budget must not be zero")
        _require_aware(self.created_at, "created_at")
        if self.finalized_at is not None:
            _require_aware(self.finalized_at, "finalized_at")
            if self.finalized_at < self.created_at:
                raise ValueError("finalized_at must not be earlier than created_at")
        if not isinstance(self.status, BudgetReservationStatus):
            raise TypeError("status must be a BudgetReservationStatus")
        _validate_budget_reservation_state(self)


def _validate_budget_reservation_state(reservation: BudgetReservation) -> None:
    for field_name in ("model_calls", "input_tokens", "output_tokens", "external_queries"):
        if getattr(reservation.actual, field_name) > getattr(reservation.reserved, field_name):
            raise ValueError("actual usage must not exceed reserved budget")
    if reservation.status is BudgetReservationStatus.ACTIVE and reservation.finalized_at is not None:
        raise ValueError("active reservation must not have finalized_at")
    if reservation.status is not BudgetReservationStatus.ACTIVE and reservation.finalized_at is None:
        raise ValueError("finalized reservation requires finalized_at")


@dataclass(frozen=True, slots=True)
class PackSnapshot:
    pack_snapshot_id: str
    pack_id: str
    pack_version: str
    manifest_digest: str
    content_artifact_id: str
    asset_version_refs: tuple[str, ...]
    created_at: datetime
    source_repository: str | None = None
    source_commit: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "pack_snapshot_id",
            "pack_id",
            "pack_version",
            "manifest_digest",
            "content_artifact_id",
        ):
            _require_text(getattr(self, field_name), field_name)
        _require_aware(self.created_at, "created_at")
        if self.source_repository is not None:
            _require_text(self.source_repository, "source_repository")
        if self.source_commit is not None:
            _require_text(self.source_commit, "source_commit")
        if len(self.asset_version_refs) != len(set(self.asset_version_refs)):
            raise ValueError("asset_version_refs must not contain duplicates")
        for asset_ref in self.asset_version_refs:
            _require_text(asset_ref, "asset_version_refs")


@dataclass(frozen=True, slots=True)
class WorkItem:
    work_id: str
    tenant_id: str
    digital_employee_id: str
    pack_id: str
    pack_version: str
    pack_snapshot_id: str
    runtime_release: str
    requester_id: str
    reviewer_id: str
    approver_id: str
    data_owner_id: str
    beneficiary_id: str
    playbook_id: str
    playbook_version: str
    budget_id: str
    idempotency_key: str
    created_at: datetime
    updated_at: datetime
    status: WorkStatus = WorkStatus.DRAFT
    current_phase: str = "intake"
    phase_attempt: int = 0
    version: int = 0
    priority: int = 0
    approval_state: str = "not_requested"
    skill_set_version: str | None = None
    skill_digests: tuple[str, ...] = ()
    input_file_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    brief: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    external_task_id: str | None = None
    next_poll_at: datetime | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    due_at: datetime | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "work_id",
            "tenant_id",
            "digital_employee_id",
            "pack_id",
            "pack_version",
            "pack_snapshot_id",
            "runtime_release",
            "requester_id",
            "reviewer_id",
            "approver_id",
            "data_owner_id",
            "beneficiary_id",
            "playbook_id",
            "playbook_version",
            "budget_id",
            "idempotency_key",
            "current_phase",
        ):
            _require_text(getattr(self, field_name), field_name)
        _require_aware(self.created_at, "created_at")
        _require_aware(self.updated_at, "updated_at")
        for field_name in ("next_poll_at", "lease_expires_at", "due_at"):
            value = getattr(self, field_name)
            if value is not None:
                _require_aware(value, field_name)
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be earlier than created_at")
        if self.phase_attempt < 0:
            raise ValueError("phase_attempt must be non-negative")
        if self.version < 0:
            raise ValueError("version must be non-negative")
        if not isinstance(self.status, WorkStatus):
            raise TypeError("status must be a WorkStatus")
        object.__setattr__(self, "brief", MappingProxyType(dict(self.brief)))

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_WORK_STATUSES


@dataclass(frozen=True, slots=True)
class WorkEvent:
    event_id: str
    work_id: str
    event_type: str
    actor_type: ActorType
    actor_id: str
    phase: str
    from_status: WorkStatus
    to_status: WorkStatus
    work_version: int
    payload_digest: str
    policy_decision: str
    occurred_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "event_id",
            "work_id",
            "event_type",
            "actor_id",
            "phase",
            "payload_digest",
            "policy_decision",
        ):
            _require_text(getattr(self, field_name), field_name)
        _require_aware(self.occurred_at, "occurred_at")
        if self.work_version <= 0:
            raise ValueError("work_version must be greater than zero")
        if not isinstance(self.actor_type, ActorType):
            raise TypeError("actor_type must be an ActorType")
