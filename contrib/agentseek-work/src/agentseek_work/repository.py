from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, NoReturn

from sqlalchemy import Engine, RowMapping, insert, select, update
from sqlalchemy.exc import IntegrityError, StatementError

from agentseek_work.models import ActorType, WorkBudget, WorkEvent, WorkItem, WorkStatus
from agentseek_work.schema import work_budgets, work_events, work_items
from agentseek_work.state_machine import OptimisticConcurrencyError, TransitionResult


class WorkRepositoryError(RuntimeError):
    """Base class for work-ledger persistence failures."""


class WorkNotFoundError(WorkRepositoryError):
    """Raised when a tenant-scoped WorkItem does not exist."""


class WorkConflictError(WorkRepositoryError):
    """Raised when a unique or append-only ledger constraint is violated."""


class NonJsonValueError(WorkRepositoryError):
    """Raised before persistence when a document contains a non-JSON value."""


@dataclass(frozen=True, slots=True)
class CreateWorkResult:
    item: WorkItem
    created: bool


class SQLAlchemyWorkRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def put_budget(self, budget_id: str, budget: WorkBudget) -> None:
        values = {
            "budget_id": budget_id,
            "max_model_calls": budget.max_model_calls,
            "max_input_tokens": budget.max_input_tokens,
            "max_output_tokens": budget.max_output_tokens,
            "max_external_queries": budget.max_external_queries,
            "max_phase_duration_seconds": budget.max_phase_duration_seconds,
            "max_work_duration_seconds": budget.max_work_duration_seconds,
            "max_retry_count": budget.max_retry_count,
        }
        try:
            with self.engine.begin() as connection:
                existing = (
                    connection
                    .execute(select(work_budgets).where(work_budgets.c.budget_id == budget_id))
                    .mappings()
                    .one_or_none()
                )
                if existing is None:
                    connection.execute(insert(work_budgets).values(**values))
                elif any(existing[key] != value for key, value in values.items()):
                    raise WorkConflictError(f"budget {budget_id} already exists with different values")
        except IntegrityError as exc:
            raise WorkConflictError(f"budget {budget_id} conflicts with an existing record") from exc

    def create_work(self, item: WorkItem) -> CreateWorkResult:
        values = _item_to_values(item)
        try:
            with self.engine.begin() as connection:
                existing = (
                    connection
                    .execute(
                        select(work_items).where(
                            work_items.c.tenant_id == item.tenant_id,
                            work_items.c.idempotency_key == item.idempotency_key,
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if existing is not None:
                    return CreateWorkResult(item=_row_to_item(existing), created=False)
                connection.execute(insert(work_items).values(**values))
        except IntegrityError as exc:
            existing = self._get_by_idempotency_key(
                tenant_id=item.tenant_id,
                idempotency_key=item.idempotency_key,
            )
            if existing is not None:
                return CreateWorkResult(item=existing, created=False)
            raise WorkConflictError("work item creation failed") from exc
        except StatementError as exc:
            self._raise_write_error(exc, "work item creation failed")
        return CreateWorkResult(item=item, created=True)

    def get_work(self, *, tenant_id: str, work_id: str) -> WorkItem:
        with self.engine.connect() as connection:
            row = (
                connection
                .execute(
                    select(work_items).where(
                        work_items.c.tenant_id == tenant_id,
                        work_items.c.work_id == work_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise WorkNotFoundError(f"work item {work_id} was not found for tenant")
        return _row_to_item(row)

    def commit_transition(
        self,
        *,
        tenant_id: str,
        expected_version: int,
        result: TransitionResult,
    ) -> WorkItem:
        item = result.item
        event = result.event
        if event.work_id != item.work_id or event.work_version != item.version:
            raise WorkConflictError("transition event does not match the WorkItem snapshot")
        if event.to_status is not item.status or event.phase != item.current_phase:
            raise WorkConflictError("transition event status or phase does not match the WorkItem snapshot")

        values = _item_to_values(item)
        values.pop("work_id")
        values.pop("tenant_id")
        try:
            with self.engine.begin() as connection:
                updated = connection.execute(
                    update(work_items)
                    .where(
                        work_items.c.tenant_id == tenant_id,
                        work_items.c.work_id == item.work_id,
                        work_items.c.version == expected_version,
                    )
                    .values(**values)
                )
                if updated.rowcount != 1:
                    _raise_optimistic_concurrency(item.work_id)
                connection.execute(insert(work_events).values(**_event_to_values(event)))
        except OptimisticConcurrencyError:
            raise
        except (IntegrityError, StatementError) as exc:
            self._raise_write_error(exc, "transition commit failed")
        return item

    def list_events(self, *, tenant_id: str, work_id: str) -> tuple[WorkEvent, ...]:
        with self.engine.connect() as connection:
            owned = connection.execute(
                select(work_items.c.work_id).where(
                    work_items.c.tenant_id == tenant_id,
                    work_items.c.work_id == work_id,
                )
            ).scalar_one_or_none()
            if owned is None:
                raise WorkNotFoundError(f"work item {work_id} was not found for tenant")
            rows = connection.execute(
                select(work_events).where(work_events.c.work_id == work_id).order_by(work_events.c.work_version)
            ).mappings()
            return tuple(_row_to_event(row) for row in rows)

    def _get_by_idempotency_key(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
    ) -> WorkItem | None:
        with self.engine.connect() as connection:
            row = (
                connection
                .execute(
                    select(work_items).where(
                        work_items.c.tenant_id == tenant_id,
                        work_items.c.idempotency_key == idempotency_key,
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _row_to_item(row)

    @staticmethod
    def _raise_write_error(exc: Exception, message: str) -> NoReturn:
        if _contains_json_error(exc):
            raise NonJsonValueError(message) from exc
        raise WorkConflictError(message) from exc


def _json_document(value: Any, field_name: str) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise NonJsonValueError(f"{field_name} must be JSON-compatible") from exc


def _item_to_values(item: WorkItem) -> dict[str, Any]:
    return {
        "work_id": item.work_id,
        "tenant_id": item.tenant_id,
        "digital_employee_id": item.digital_employee_id,
        "pack_id": item.pack_id,
        "pack_version": item.pack_version,
        "pack_snapshot_id": item.pack_snapshot_id,
        "skill_set_version": item.skill_set_version,
        "skill_digests": _json_document(list(item.skill_digests), "skill_digests"),
        "runtime_release": item.runtime_release,
        "requester_id": item.requester_id,
        "reviewer_id": item.reviewer_id,
        "approver_id": item.approver_id,
        "data_owner_id": item.data_owner_id,
        "beneficiary_id": item.beneficiary_id,
        "playbook_id": item.playbook_id,
        "playbook_version": item.playbook_version,
        "brief": _json_document(dict(item.brief), "brief"),
        "status": item.status.value,
        "current_phase": item.current_phase,
        "phase_attempt": item.phase_attempt,
        "version": item.version,
        "priority": item.priority,
        "input_file_ids": _json_document(list(item.input_file_ids), "input_file_ids"),
        "source_ids": _json_document(list(item.source_ids), "source_ids"),
        "artifact_ids": _json_document(list(item.artifact_ids), "artifact_ids"),
        "approval_state": item.approval_state,
        "budget_id": item.budget_id,
        "external_task_id": item.external_task_id,
        "next_poll_at": item.next_poll_at,
        "lease_owner": item.lease_owner,
        "lease_expires_at": item.lease_expires_at,
        "due_at": item.due_at,
        "idempotency_key": item.idempotency_key,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def _event_to_values(event: WorkEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "work_id": event.work_id,
        "event_type": event.event_type,
        "actor_type": event.actor_type.value,
        "actor_id": event.actor_id,
        "phase": event.phase,
        "from_status": event.from_status.value,
        "to_status": event.to_status.value,
        "work_version": event.work_version,
        "payload_digest": event.payload_digest,
        "policy_decision": event.policy_decision,
        "occurred_at": event.occurred_at,
    }


def _row_to_item(row: RowMapping | Mapping[str, Any]) -> WorkItem:
    return WorkItem(
        work_id=str(row["work_id"]),
        tenant_id=str(row["tenant_id"]),
        digital_employee_id=str(row["digital_employee_id"]),
        pack_id=str(row["pack_id"]),
        pack_version=str(row["pack_version"]),
        pack_snapshot_id=str(row["pack_snapshot_id"]),
        skill_set_version=_optional_text(row["skill_set_version"]),
        skill_digests=tuple(str(value) for value in row["skill_digests"]),
        runtime_release=str(row["runtime_release"]),
        requester_id=str(row["requester_id"]),
        reviewer_id=str(row["reviewer_id"]),
        approver_id=str(row["approver_id"]),
        data_owner_id=str(row["data_owner_id"]),
        beneficiary_id=str(row["beneficiary_id"]),
        playbook_id=str(row["playbook_id"]),
        playbook_version=str(row["playbook_version"]),
        brief=dict(row["brief"]),
        status=WorkStatus(str(row["status"])),
        current_phase=str(row["current_phase"]),
        phase_attempt=int(row["phase_attempt"]),
        version=int(row["version"]),
        priority=int(row["priority"]),
        input_file_ids=tuple(str(value) for value in row["input_file_ids"]),
        source_ids=tuple(str(value) for value in row["source_ids"]),
        artifact_ids=tuple(str(value) for value in row["artifact_ids"]),
        approval_state=str(row["approval_state"]),
        budget_id=str(row["budget_id"]),
        external_task_id=_optional_text(row["external_task_id"]),
        next_poll_at=_optional_datetime(row["next_poll_at"]),
        lease_owner=_optional_text(row["lease_owner"]),
        lease_expires_at=_optional_datetime(row["lease_expires_at"]),
        due_at=_optional_datetime(row["due_at"]),
        idempotency_key=str(row["idempotency_key"]),
        created_at=_aware_datetime(row["created_at"]),
        updated_at=_aware_datetime(row["updated_at"]),
    )


def _row_to_event(row: RowMapping | Mapping[str, Any]) -> WorkEvent:
    return WorkEvent(
        event_id=str(row["event_id"]),
        work_id=str(row["work_id"]),
        event_type=str(row["event_type"]),
        actor_type=ActorType(str(row["actor_type"])),
        actor_id=str(row["actor_id"]),
        phase=str(row["phase"]),
        from_status=WorkStatus(str(row["from_status"])),
        to_status=WorkStatus(str(row["to_status"])),
        work_version=int(row["work_version"]),
        payload_digest=str(row["payload_digest"]),
        policy_decision=str(row["policy_decision"]),
        occurred_at=_aware_datetime(row["occurred_at"]),
    )


def _aware_datetime(value: Any) -> datetime:
    if not isinstance(value, datetime):
        raise WorkRepositoryError("database timestamp has an invalid type")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value


def _optional_datetime(value: Any) -> datetime | None:
    return None if value is None else _aware_datetime(value)


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _contains_json_error(exc: Exception) -> bool:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, (TypeError, ValueError)):
            return True
        current = current.__cause__ or current.__context__
    return False


def _raise_optimistic_concurrency(work_id: str) -> NoReturn:
    raise OptimisticConcurrencyError(f"work item version mismatch or tenant scope denied for {work_id}")
