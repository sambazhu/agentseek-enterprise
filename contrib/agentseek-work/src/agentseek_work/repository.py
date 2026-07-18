from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn

from sqlalchemy import Connection, Engine, RowMapping, insert, select, update
from sqlalchemy.exc import IntegrityError, StatementError

from agentseek_work.models import (
    TERMINAL_WORK_STATUSES,
    ActorType,
    BudgetAmount,
    BudgetReservation,
    BudgetReservationStatus,
    BudgetUsage,
    ClaimRecord,
    ClaimReviewerStatus,
    ClaimType,
    ClaimVerificationStatus,
    EvidenceRecord,
    ExcerptStatus,
    PackSnapshot,
    SnapshotStatus,
    SourceRecord,
    SourceType,
    WorkBudget,
    WorkContractSnapshot,
    WorkContractStatus,
    WorkEvent,
    WorkItem,
    WorkStatus,
)
from agentseek_work.schema import (
    pack_snapshots,
    work_budget_reservations,
    work_budget_usage,
    work_budgets,
    work_claim_evidence,
    work_claims,
    work_contracts,
    work_events,
    work_evidence,
    work_items,
    work_sources,
)
from agentseek_work.state_machine import OptimisticConcurrencyError, TransitionResult


class WorkRepositoryError(RuntimeError):
    """Base class for work-ledger persistence failures."""


class WorkNotFoundError(WorkRepositoryError):
    """Raised when a tenant-scoped WorkItem does not exist."""


class WorkConflictError(WorkRepositoryError):
    """Raised when a unique or append-only ledger constraint is violated."""


class ActiveWorkConflictError(WorkConflictError):
    """Raised when a different request targets an already-active playbook scope."""

    def __init__(self, existing: WorkItem) -> None:
        self.existing = existing
        super().__init__(
            "an active WorkItem already exists for this tenant, requester, "
            "digital employee, and playbook"
        )


class WorkContractConflictError(WorkConflictError):
    """Raised when a versioned WorkItem contract violates its lifecycle contract."""


class NonJsonValueError(WorkRepositoryError):
    """Raised before persistence when a document contains a non-JSON value."""


class BudgetExceededError(WorkRepositoryError):
    """Raised before a call when its reservation would exceed the frozen budget."""


class BudgetReservationError(WorkRepositoryError):
    """Raised when a reservation cannot be settled or released safely."""


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

    def get_budget_for_work(self, *, tenant_id: str, work_id: str) -> WorkBudget:
        with self.engine.connect() as connection:
            row = (
                connection
                .execute(
                    select(work_budgets)
                    .join(work_items, work_items.c.budget_id == work_budgets.c.budget_id)
                    .where(
                        work_items.c.tenant_id == tenant_id,
                        work_items.c.work_id == work_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise WorkNotFoundError(f"work item {work_id} was not found for tenant")
        return WorkBudget(
            max_model_calls=int(row["max_model_calls"]),
            max_input_tokens=int(row["max_input_tokens"]),
            max_output_tokens=int(row["max_output_tokens"]),
            max_external_queries=int(row["max_external_queries"]),
            max_phase_duration_seconds=int(row["max_phase_duration_seconds"]),
            max_work_duration_seconds=int(row["max_work_duration_seconds"]),
            max_retry_count=int(row["max_retry_count"]),
        )

    def put_pack_snapshot(self, snapshot: PackSnapshot) -> PackSnapshot:
        values = _pack_snapshot_values(snapshot)
        try:
            with self.engine.begin() as connection:
                existing = (
                    connection
                    .execute(
                        select(pack_snapshots).where(pack_snapshots.c.pack_snapshot_id == snapshot.pack_snapshot_id)
                    )
                    .mappings()
                    .one_or_none()
                )
                if existing is None:
                    connection.execute(insert(pack_snapshots).values(**values))
                    return snapshot
                stored = _row_to_pack_snapshot(existing)
                if stored != snapshot:
                    raise WorkConflictError(
                        f"pack snapshot {snapshot.pack_snapshot_id} already exists with different values"
                    )
                return stored
        except IntegrityError as exc:
            raise WorkConflictError("pack snapshot conflicts with an existing immutable version") from exc
        except StatementError as exc:
            self._raise_write_error(exc, "pack snapshot creation failed")

    def get_pack_snapshot(self, *, pack_snapshot_id: str) -> PackSnapshot:
        with self.engine.connect() as connection:
            row = (
                connection
                .execute(select(pack_snapshots).where(pack_snapshots.c.pack_snapshot_id == pack_snapshot_id))
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise WorkNotFoundError(f"pack snapshot {pack_snapshot_id} was not found")
        return _row_to_pack_snapshot(row)

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
                active = _find_active_work(
                    connection,
                    tenant_id=item.tenant_id,
                    requester_id=item.requester_id,
                    digital_employee_id=item.digital_employee_id,
                    playbook_id=item.playbook_id,
                )
                if active is not None:
                    raise ActiveWorkConflictError(active)
                _require_pack_snapshot_binding(connection, item)
                connection.execute(insert(work_items).values(**values))
        except IntegrityError as exc:
            existing = self._get_by_idempotency_key(
                tenant_id=item.tenant_id,
                idempotency_key=item.idempotency_key,
            )
            if existing is not None:
                return CreateWorkResult(item=existing, created=False)
            active = self.find_active_work(
                tenant_id=item.tenant_id,
                requester_id=item.requester_id,
                digital_employee_id=item.digital_employee_id,
                playbook_id=item.playbook_id,
            )
            if active is not None:
                raise ActiveWorkConflictError(active) from exc
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

    def find_current_work(
        self,
        *,
        tenant_id: str,
        requester_id: str,
        digital_employee_id: str,
    ) -> WorkItem | None:
        """Return the requester's most recently updated non-terminal WorkItem."""

        with self.engine.connect() as connection:
            row = (
                connection
                .execute(
                    select(work_items)
                    .where(
                        work_items.c.tenant_id == tenant_id,
                        work_items.c.requester_id == requester_id,
                        work_items.c.digital_employee_id == digital_employee_id,
                        work_items.c.status.not_in(_terminal_status_values()),
                    )
                    .order_by(work_items.c.updated_at.desc(), work_items.c.work_id.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
        return _row_to_item(row) if row is not None else None

    def find_active_work(
        self,
        *,
        tenant_id: str,
        requester_id: str,
        digital_employee_id: str,
        playbook_id: str,
    ) -> WorkItem | None:
        """Return the active WorkItem for one concurrency-controlled playbook scope."""

        with self.engine.connect() as connection:
            return _find_active_work(
                connection,
                tenant_id=tenant_id,
                requester_id=requester_id,
                digital_employee_id=digital_employee_id,
                playbook_id=playbook_id,
            )

    def create_work_contract(self, contract: WorkContractSnapshot) -> WorkContractSnapshot:
        """Create the first provisional contract for a WorkItem, idempotently."""

        if contract.contract_version != 1:
            raise ValueError("the first contract_version must be 1")
        if contract.status is not WorkContractStatus.PROVISIONAL:
            raise ValueError("the first WorkItem contract must be provisional")
        try:
            with self.engine.begin() as connection:
                return _create_work_contract(connection, contract)
        except WorkContractConflictError:
            raise
        except IntegrityError as exc:
            raise WorkContractConflictError("WorkItem contract creation failed") from exc
        except StatementError as exc:
            self._raise_write_error(exc, "WorkItem contract creation failed")

    def get_current_work_contract(
        self,
        *,
        tenant_id: str,
        work_id: str,
        contract_type: str,
    ) -> WorkContractSnapshot | None:
        with self.engine.connect() as connection:
            return _find_current_contract(
                connection,
                tenant_id=tenant_id,
                work_id=work_id,
                contract_type=contract_type,
            )

    def list_work_contracts(
        self,
        *,
        tenant_id: str,
        work_id: str,
        contract_type: str,
    ) -> tuple[WorkContractSnapshot, ...]:
        with self.engine.connect() as connection:
            _require_owned_work(connection, tenant_id=tenant_id, work_id=work_id)
            rows = (
                connection
                .execute(
                    select(work_contracts)
                    .where(
                        work_contracts.c.tenant_id == tenant_id,
                        work_contracts.c.work_id == work_id,
                        work_contracts.c.contract_type == contract_type,
                    )
                    .order_by(work_contracts.c.contract_version)
                )
                .mappings()
                .all()
            )
        return tuple(_row_to_contract(row) for row in rows)

    def confirm_work_contract(
        self,
        *,
        tenant_id: str,
        work_id: str,
        contract_type: str,
        expected_contract_version: int,
        confirmed_by: str,
        confirmed_at: datetime,
    ) -> WorkContractSnapshot:
        _require_aware_datetime(confirmed_at, "confirmed_at")
        with self.engine.begin() as connection:
            requester_id = _require_owned_work(connection, tenant_id=tenant_id, work_id=work_id)
            if confirmed_by != requester_id:
                raise WorkContractConflictError("only the WorkItem requester can confirm its contract")
            current = _find_current_contract(
                connection,
                tenant_id=tenant_id,
                work_id=work_id,
                contract_type=contract_type,
                for_update=True,
            )
            if current is None:
                raise WorkNotFoundError("current WorkItem contract was not found for tenant")
            if current.contract_version != expected_contract_version:
                raise WorkContractConflictError("WorkItem contract version mismatch")
            if current.status is WorkContractStatus.CONFIRMED:
                if current.confirmed_by == confirmed_by:
                    return current
                raise WorkContractConflictError("WorkItem contract was confirmed by another actor")
            if current.status is not WorkContractStatus.PROVISIONAL:
                raise WorkContractConflictError("only a provisional WorkItem contract can be confirmed")
            confirmed = replace(
                current,
                status=WorkContractStatus.CONFIRMED,
                confirmed_by=confirmed_by,
                confirmed_at=confirmed_at,
            )
            updated = connection.execute(
                update(work_contracts)
                .where(
                    work_contracts.c.work_id == work_id,
                    work_contracts.c.contract_type == contract_type,
                    work_contracts.c.contract_version == expected_contract_version,
                    work_contracts.c.status == WorkContractStatus.PROVISIONAL.value,
                )
                .values(**_contract_lifecycle_values(confirmed))
            )
            if updated.rowcount != 1:
                raise WorkContractConflictError("WorkItem contract confirmation lost a concurrent update")
            return confirmed

    def revise_work_contract(self, contract: WorkContractSnapshot) -> WorkContractSnapshot:
        """Supersede the current contract and append its next provisional version atomically."""

        if contract.status is not WorkContractStatus.PROVISIONAL:
            raise ValueError("a revised WorkItem contract must start provisional")
        try:
            with self.engine.begin() as connection:
                return _revise_work_contract(connection, contract)
        except (WorkContractConflictError, WorkNotFoundError):
            raise
        except IntegrityError as exc:
            raise WorkContractConflictError("WorkItem contract revision failed") from exc
        except StatementError as exc:
            self._raise_write_error(exc, "WorkItem contract revision failed")

    def put_source_record(self, source: SourceRecord) -> SourceRecord:
        """Register immutable source provenance for a tenant-owned WorkItem, idempotently."""

        values = _source_values(source)
        try:
            with self.engine.begin() as connection:
                _require_owned_work(connection, tenant_id=source.tenant_id, work_id=source.work_id)
                existing = (
                    connection
                    .execute(
                        select(work_sources).where(
                            work_sources.c.tenant_id == source.tenant_id,
                            work_sources.c.source_id == source.source_id,
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if existing is not None:
                    stored = _row_to_source(existing)
                    if stored != source:
                        _raise_source_record_conflict(source.source_id)
                    return stored
                connection.execute(insert(work_sources).values(**values))
                return source
        except WorkConflictError:
            raise
        except IntegrityError as exc:
            raise WorkConflictError("source record conflicts with an existing immutable record") from exc
        except StatementError as exc:
            self._raise_write_error(exc, "source record creation failed")

    def get_source_record(self, *, tenant_id: str, source_id: str) -> SourceRecord:
        with self.engine.connect() as connection:
            row = (
                connection
                .execute(
                    select(work_sources).where(
                        work_sources.c.tenant_id == tenant_id,
                        work_sources.c.source_id == source_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise WorkNotFoundError(f"source record {source_id} was not found for tenant")
        return _row_to_source(row)

    def list_source_records(self, *, tenant_id: str, work_id: str) -> tuple[SourceRecord, ...]:
        with self.engine.connect() as connection:
            _require_owned_work(connection, tenant_id=tenant_id, work_id=work_id)
            rows = (
                connection
                .execute(
                    select(work_sources)
                    .where(
                        work_sources.c.tenant_id == tenant_id,
                        work_sources.c.work_id == work_id,
                    )
                    .order_by(work_sources.c.retrieved_at, work_sources.c.source_id)
                )
                .mappings()
                .all()
            )
        return tuple(_row_to_source(row) for row in rows)

    def put_evidence_record(self, evidence: EvidenceRecord) -> EvidenceRecord:
        """Register immutable source-derived evidence for a tenant-owned WorkItem."""

        values = _evidence_values(evidence)
        try:
            with self.engine.begin() as connection:
                _require_owned_work(connection, tenant_id=evidence.tenant_id, work_id=evidence.work_id)
                _require_source_binding(connection, evidence)
                existing = (
                    connection
                    .execute(
                        select(work_evidence).where(
                            work_evidence.c.tenant_id == evidence.tenant_id,
                            work_evidence.c.evidence_id == evidence.evidence_id,
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if existing is not None:
                    stored = _row_to_evidence(existing)
                    if stored != evidence:
                        _raise_evidence_record_conflict(evidence.evidence_id)
                    return stored
                connection.execute(insert(work_evidence).values(**values))
                return evidence
        except WorkConflictError:
            raise
        except IntegrityError as exc:
            raise WorkConflictError("evidence record conflicts with an existing immutable record") from exc
        except StatementError as exc:
            self._raise_write_error(exc, "evidence record creation failed")

    def get_evidence_record(self, *, tenant_id: str, evidence_id: str) -> EvidenceRecord:
        with self.engine.connect() as connection:
            row = (
                connection
                .execute(
                    select(work_evidence).where(
                        work_evidence.c.tenant_id == tenant_id,
                        work_evidence.c.evidence_id == evidence_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise WorkNotFoundError(f"evidence record {evidence_id} was not found for tenant")
        return _row_to_evidence(row)

    def list_evidence_records(self, *, tenant_id: str, work_id: str) -> tuple[EvidenceRecord, ...]:
        with self.engine.connect() as connection:
            _require_owned_work(connection, tenant_id=tenant_id, work_id=work_id)
            rows = (
                connection
                .execute(
                    select(work_evidence)
                    .where(
                        work_evidence.c.tenant_id == tenant_id,
                        work_evidence.c.work_id == work_id,
                    )
                    .order_by(work_evidence.c.created_at, work_evidence.c.evidence_id)
                )
                .mappings()
                .all()
            )
        return tuple(_row_to_evidence(row) for row in rows)

    def put_claim_record(self, claim: ClaimRecord) -> ClaimRecord:
        """Register an immutable claim and its ordered EvidenceRecord bindings."""

        values = _claim_values(claim)
        try:
            with self.engine.begin() as connection:
                _require_owned_work(connection, tenant_id=claim.tenant_id, work_id=claim.work_id)
                existing = (
                    connection
                    .execute(
                        select(work_claims).where(
                            work_claims.c.tenant_id == claim.tenant_id,
                            work_claims.c.claim_id == claim.claim_id,
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if existing is not None:
                    stored = _row_to_claim(connection, existing)
                    if stored != claim:
                        _raise_claim_record_conflict(claim.claim_id)
                    return stored
                _require_claim_evidence_bindings(connection, claim)
                connection.execute(insert(work_claims).values(**values))
                if claim.evidence_ids:
                    connection.execute(
                        insert(work_claim_evidence),
                        [
                            {"claim_id": claim.claim_id, "evidence_id": evidence_id, "ordinal": ordinal}
                            for ordinal, evidence_id in enumerate(claim.evidence_ids)
                        ],
                    )
                return claim
        except WorkConflictError:
            raise
        except IntegrityError as exc:
            raise WorkConflictError("claim record conflicts with an existing immutable record") from exc
        except StatementError as exc:
            self._raise_write_error(exc, "claim record creation failed")

    def get_claim_record(self, *, tenant_id: str, claim_id: str) -> ClaimRecord:
        with self.engine.connect() as connection:
            row = (
                connection
                .execute(
                    select(work_claims).where(
                        work_claims.c.tenant_id == tenant_id,
                        work_claims.c.claim_id == claim_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise WorkNotFoundError(f"claim record {claim_id} was not found for tenant")
            return _row_to_claim(connection, row)

    def list_claim_records(self, *, tenant_id: str, work_id: str) -> tuple[ClaimRecord, ...]:
        with self.engine.connect() as connection:
            _require_owned_work(connection, tenant_id=tenant_id, work_id=work_id)
            rows = (
                connection
                .execute(
                    select(work_claims)
                    .where(
                        work_claims.c.tenant_id == tenant_id,
                        work_claims.c.work_id == work_id,
                    )
                    .order_by(work_claims.c.created_at, work_claims.c.claim_id)
                )
                .mappings()
                .all()
            )
            return tuple(_row_to_claim(connection, row) for row in rows)

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

    def claim_next(
        self,
        *,
        worker_id: str,
        event_id: str,
        now: datetime,
        lease_duration: timedelta,
        tenant_id: str | None = None,
    ) -> WorkItem | None:
        _validate_lease_request(worker_id, now, lease_duration)
        with self.engine.begin() as connection:
            query = select(work_items).where(work_items.c.status == WorkStatus.QUEUED.value)
            if tenant_id is not None:
                query = query.where(work_items.c.tenant_id == tenant_id)
            row = (
                connection
                .execute(
                    query
                    .order_by(work_items.c.priority.desc(), work_items.c.created_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            item = _row_to_item(row)
            result = _system_transition(
                item,
                to_status=WorkStatus.RUNNING,
                event_id=event_id,
                event_type="work_claimed",
                actor_id=worker_id,
                occurred_at=now,
            )
            claimed = replace(
                result.item,
                lease_owner=worker_id,
                lease_expires_at=now + lease_duration,
            )
            _update_snapshot(connection, item=item, updated=claimed)
            connection.execute(insert(work_events).values(**_event_to_values(result.event)))
            return claimed

    def renew_lease(
        self,
        *,
        tenant_id: str,
        work_id: str,
        worker_id: str,
        now: datetime,
        lease_duration: timedelta,
    ) -> WorkItem:
        _validate_lease_request(worker_id, now, lease_duration)
        expires_at = now + lease_duration
        with self.engine.begin() as connection:
            updated = connection.execute(
                update(work_items)
                .where(
                    work_items.c.tenant_id == tenant_id,
                    work_items.c.work_id == work_id,
                    work_items.c.status == WorkStatus.RUNNING.value,
                    work_items.c.lease_owner == worker_id,
                    work_items.c.lease_expires_at > now,
                )
                .values(lease_expires_at=expires_at)
            )
            if updated.rowcount != 1:
                raise WorkConflictError("lease is missing, expired, or owned by another worker")
        return self.get_work(tenant_id=tenant_id, work_id=work_id)

    def abandon_lease(
        self,
        *,
        tenant_id: str,
        work_id: str,
        worker_id: str,
        now: datetime,
    ) -> bool:
        _require_aware_datetime(now, "now")
        with self.engine.begin() as connection:
            updated = connection.execute(
                update(work_items)
                .where(
                    work_items.c.tenant_id == tenant_id,
                    work_items.c.work_id == work_id,
                    work_items.c.status == WorkStatus.RUNNING.value,
                    work_items.c.lease_owner == worker_id,
                )
                .values(lease_expires_at=now)
            )
            return updated.rowcount == 1

    def commit_worker_transition(
        self,
        *,
        tenant_id: str,
        worker_id: str,
        expected_version: int,
        now: datetime,
        result: TransitionResult,
    ) -> WorkItem:
        item = result.item
        event = result.event
        if event.work_id != item.work_id or event.work_version != item.version:
            raise WorkConflictError("transition event does not match the WorkItem snapshot")
        if event.to_status is not item.status or event.phase != item.current_phase:
            raise WorkConflictError("transition event status or phase does not match the WorkItem snapshot")
        _require_aware_datetime(now, "now")
        committed = replace(item, lease_owner=None, lease_expires_at=None)
        values = _item_to_values(committed)
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
                        work_items.c.status == WorkStatus.RUNNING.value,
                        work_items.c.lease_owner == worker_id,
                        work_items.c.lease_expires_at > now,
                    )
                    .values(**values)
                )
                if updated.rowcount != 1:
                    _raise_worker_lease_required()
                active_reservation = connection.execute(
                    select(work_budget_reservations.c.reservation_id)
                    .where(
                        work_budget_reservations.c.work_id == item.work_id,
                        work_budget_reservations.c.status == BudgetReservationStatus.ACTIVE.value,
                    )
                    .limit(1)
                ).scalar_one_or_none()
                if active_reservation is not None:
                    raise BudgetReservationError("active budget reservations must be finalized before phase commit")
                connection.execute(insert(work_events).values(**_event_to_values(event)))
        except WorkConflictError:
            raise
        except (IntegrityError, StatementError) as exc:
            self._raise_write_error(exc, "worker transition commit failed")
        return committed

    def requeue_due_external(
        self,
        *,
        event_id: str,
        now: datetime,
        tenant_id: str | None = None,
    ) -> WorkItem | None:
        _require_aware_datetime(now, "now")
        with self.engine.begin() as connection:
            query = select(work_items).where(
                work_items.c.status == WorkStatus.WAITING_EXTERNAL.value,
                work_items.c.next_poll_at.is_not(None),
                work_items.c.next_poll_at <= now,
            )
            if tenant_id is not None:
                query = query.where(work_items.c.tenant_id == tenant_id)
            row = (
                connection
                .execute(query.order_by(work_items.c.next_poll_at).limit(1).with_for_update(skip_locked=True))
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            item = _row_to_item(row)
            result = _system_transition(
                item,
                to_status=WorkStatus.QUEUED,
                event_id=event_id,
                event_type="external_poll_due",
                actor_id="scheduler",
                occurred_at=now,
            )
            queued = replace(result.item, next_poll_at=None)
            _update_snapshot(connection, item=item, updated=queued)
            connection.execute(insert(work_events).values(**_event_to_values(result.event)))
            return queued

    def recover_expired_lease(
        self,
        *,
        worker_id: str,
        event_id: str,
        now: datetime,
        lease_duration: timedelta,
        tenant_id: str | None = None,
    ) -> WorkItem | None:
        _validate_lease_request(worker_id, now, lease_duration)
        with self.engine.begin() as connection:
            query = (
                select(work_items, work_budgets.c.max_retry_count)
                .join(work_budgets, work_items.c.budget_id == work_budgets.c.budget_id)
                .where(
                    work_items.c.status == WorkStatus.RUNNING.value,
                    work_items.c.lease_expires_at.is_not(None),
                    work_items.c.lease_expires_at <= now,
                )
            )
            if tenant_id is not None:
                query = query.where(work_items.c.tenant_id == tenant_id)
            row = (
                connection
                .execute(query.order_by(work_items.c.lease_expires_at).limit(1).with_for_update(skip_locked=True))
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            item = _row_to_item(row)
            max_retry_count = int(row["max_retry_count"])
            _forfeit_active_reservations(connection, item=item, now=now)
            if item.phase_attempt <= max_retry_count:
                recovered = replace(
                    item,
                    phase_attempt=item.phase_attempt + 1,
                    version=item.version + 1,
                    updated_at=now,
                    lease_owner=worker_id,
                    lease_expires_at=now + lease_duration,
                )
                event = _operational_event(
                    item=item,
                    updated=recovered,
                    event_id=event_id,
                    event_type="lease_recovered",
                    actor_id=worker_id,
                    occurred_at=now,
                )
            else:
                result = _system_transition(
                    item,
                    to_status=WorkStatus.FAILED,
                    event_id=event_id,
                    event_type="retry_exhausted",
                    actor_id=worker_id,
                    occurred_at=now,
                )
                recovered = replace(result.item, lease_owner=None, lease_expires_at=None)
                event = result.event
            _update_snapshot(connection, item=item, updated=recovered)
            connection.execute(insert(work_events).values(**_event_to_values(event)))
            return recovered

    def cancel_work(
        self,
        *,
        tenant_id: str,
        work_id: str,
        event_id: str,
        actor_type: ActorType,
        actor_id: str,
        now: datetime,
    ) -> WorkItem:
        _require_aware_datetime(now, "now")
        with self.engine.begin() as connection:
            row = (
                connection
                .execute(
                    select(work_items)
                    .where(
                        work_items.c.tenant_id == tenant_id,
                        work_items.c.work_id == work_id,
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise WorkNotFoundError(f"work item {work_id} was not found for tenant")
            item = _row_to_item(row)
            _forfeit_active_reservations(connection, item=item, now=now)
            from agentseek_work.state_machine import transition_work_item

            result = transition_work_item(
                item,
                to_status=WorkStatus.CANCELLED,
                expected_version=item.version,
                event_id=event_id,
                event_type="work_cancelled",
                actor_type=actor_type,
                actor_id=actor_id,
                occurred_at=now,
                payload_digest="sha256:cancelled",
                policy_decision="allowed",
            )
            cancelled = replace(result.item, lease_owner=None, lease_expires_at=None)
            _update_snapshot(connection, item=item, updated=cancelled)
            connection.execute(insert(work_events).values(**_event_to_values(result.event)))
            return cancelled

    def reserve_budget(
        self,
        *,
        tenant_id: str,
        work_id: str,
        worker_id: str,
        reservation_id: str,
        idempotency_key: str,
        amount: BudgetAmount,
        now: datetime,
    ) -> BudgetReservation:
        _require_aware_datetime(now, "now")
        if amount.is_zero:
            raise ValueError("budget reservation amount must not be zero")
        for value, field_name in (
            (worker_id, "worker_id"),
            (reservation_id, "reservation_id"),
            (idempotency_key, "idempotency_key"),
        ):
            if not value.strip():
                raise ValueError(f"{field_name} must not be blank")

        with self.engine.begin() as connection:
            row = (
                connection
                .execute(
                    select(work_items, work_budgets)
                    .join(work_budgets, work_items.c.budget_id == work_budgets.c.budget_id)
                    .where(
                        work_items.c.tenant_id == tenant_id,
                        work_items.c.work_id == work_id,
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise WorkNotFoundError(f"work item {work_id} was not found for tenant")
            item = _row_to_item(row)
            _require_active_worker(item, worker_id=worker_id, now=now)
            if now >= item.created_at + timedelta(seconds=int(row["max_work_duration_seconds"])):
                raise BudgetExceededError("max_work_duration_seconds is exhausted")

            existing = (
                connection
                .execute(
                    select(work_budget_reservations).where(
                        work_budget_reservations.c.work_id == work_id,
                        work_budget_reservations.c.idempotency_key == idempotency_key,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                reservation = _row_to_budget_reservation(existing)
                if (
                    reservation.reservation_id != reservation_id
                    or reservation.worker_id != worker_id
                    or reservation.phase != item.current_phase
                    or reservation.phase_attempt != item.phase_attempt
                    or reservation.reserved != amount
                ):
                    raise BudgetReservationError("budget reservation idempotency key was reused with different values")
                return reservation

            usage = _locked_budget_usage(connection, item=item, now=now)
            _require_budget_capacity(usage=usage, requested=amount, budget_row=row)
            reservation = BudgetReservation(
                reservation_id=reservation_id,
                work_id=work_id,
                tenant_id=tenant_id,
                worker_id=worker_id,
                phase=item.current_phase,
                phase_attempt=item.phase_attempt,
                idempotency_key=idempotency_key,
                status=BudgetReservationStatus.ACTIVE,
                reserved=amount,
                actual=BudgetAmount(),
                created_at=now,
            )
            connection.execute(insert(work_budget_reservations).values(**_budget_reservation_values(reservation)))
            connection.execute(
                update(work_budget_usage)
                .where(work_budget_usage.c.work_id == work_id)
                .values(**_usage_update_values(usage.used, _add_amount(usage.reserved, amount), now))
            )
            return reservation

    def settle_budget(
        self,
        *,
        reservation_id: str,
        tenant_id: str,
        worker_id: str,
        actual: BudgetAmount,
        now: datetime,
    ) -> BudgetReservation:
        return self._finalize_budget_reservation(
            reservation_id=reservation_id,
            tenant_id=tenant_id,
            worker_id=worker_id,
            actual=actual,
            status=BudgetReservationStatus.SETTLED,
            now=now,
        )

    def release_budget(
        self,
        *,
        reservation_id: str,
        tenant_id: str,
        worker_id: str,
        now: datetime,
    ) -> BudgetReservation:
        return self._finalize_budget_reservation(
            reservation_id=reservation_id,
            tenant_id=tenant_id,
            worker_id=worker_id,
            actual=BudgetAmount(),
            status=BudgetReservationStatus.RELEASED,
            now=now,
        )

    def get_budget_usage(self, *, tenant_id: str, work_id: str) -> BudgetUsage:
        with self.engine.connect() as connection:
            owned = connection.execute(
                select(work_items.c.work_id).where(
                    work_items.c.tenant_id == tenant_id,
                    work_items.c.work_id == work_id,
                )
            ).scalar_one_or_none()
            if owned is None:
                raise WorkNotFoundError(f"work item {work_id} was not found for tenant")
            row = (
                connection
                .execute(select(work_budget_usage).where(work_budget_usage.c.work_id == work_id))
                .mappings()
                .one_or_none()
            )
        return BudgetUsage() if row is None else _row_to_budget_usage(row)

    def get_budget_reservation(
        self,
        *,
        tenant_id: str,
        reservation_id: str,
    ) -> BudgetReservation:
        with self.engine.connect() as connection:
            row = (
                connection
                .execute(
                    select(work_budget_reservations).where(
                        work_budget_reservations.c.tenant_id == tenant_id,
                        work_budget_reservations.c.reservation_id == reservation_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise WorkNotFoundError("budget reservation was not found for tenant")
        return _row_to_budget_reservation(row)

    def _finalize_budget_reservation(
        self,
        *,
        reservation_id: str,
        tenant_id: str,
        worker_id: str,
        actual: BudgetAmount,
        status: BudgetReservationStatus,
        now: datetime,
    ) -> BudgetReservation:
        _require_aware_datetime(now, "now")
        with self.engine.begin() as connection:
            row = (
                connection
                .execute(
                    select(work_budget_reservations)
                    .where(
                        work_budget_reservations.c.tenant_id == tenant_id,
                        work_budget_reservations.c.reservation_id == reservation_id,
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise WorkNotFoundError("budget reservation was not found for tenant")
            reservation = _row_to_budget_reservation(row)
            if reservation.status is status and reservation.actual == actual:
                return reservation
            if reservation.status is not BudgetReservationStatus.ACTIVE:
                raise BudgetReservationError("budget reservation is already finalized")
            if worker_id != reservation.worker_id:
                raise BudgetReservationError("budget reservation is owned by another worker")
            item_row = (
                connection
                .execute(
                    select(work_items)
                    .where(
                        work_items.c.tenant_id == tenant_id,
                        work_items.c.work_id == reservation.work_id,
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if item_row is None:
                raise WorkNotFoundError(f"work item {reservation.work_id} was not found for tenant")
            item = _row_to_item(item_row)
            _require_active_worker(item, worker_id=worker_id, now=now)
            if not _amount_within(actual, reservation.reserved):
                raise BudgetReservationError("actual usage exceeds the reserved budget ceiling")
            usage = _locked_budget_usage(connection, item=item, now=now)
            used = _add_amount(usage.used, actual)
            reserved = _subtract_amount(usage.reserved, reservation.reserved)
            finalized = replace(reservation, status=status, actual=actual, finalized_at=now)
            connection.execute(
                update(work_budget_reservations)
                .where(work_budget_reservations.c.reservation_id == reservation_id)
                .values(**_budget_reservation_values(finalized))
            )
            connection.execute(
                update(work_budget_usage)
                .where(work_budget_usage.c.work_id == reservation.work_id)
                .values(**_usage_update_values(used, reserved, now))
            )
            return finalized

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


def _terminal_status_values() -> tuple[str, ...]:
    return tuple(status.value for status in TERMINAL_WORK_STATUSES)


def _raise_source_record_conflict(source_id: str) -> NoReturn:
    raise WorkConflictError(f"source record {source_id} already exists with different values")


def _raise_evidence_record_conflict(evidence_id: str) -> NoReturn:
    raise WorkConflictError(f"evidence record {evidence_id} already exists with different values")


def _raise_claim_record_conflict(claim_id: str) -> NoReturn:
    raise WorkConflictError(f"claim record {claim_id} already exists with different values")


def _require_source_binding(connection: Connection, evidence: EvidenceRecord) -> None:
    row = (
        connection
        .execute(
            select(work_sources.c.work_id).where(
                work_sources.c.tenant_id == evidence.tenant_id,
                work_sources.c.source_id == evidence.source_id,
            )
        )
        .one_or_none()
    )
    if row is None:
        raise WorkConflictError("evidence source is not registered for this tenant")
    if str(row.work_id) != evidence.work_id:
        raise WorkConflictError("evidence source belongs to a different WorkItem")


def _require_claim_evidence_bindings(connection: Connection, claim: ClaimRecord) -> None:
    if not claim.evidence_ids:
        return
    rows = connection.execute(
        select(work_evidence.c.evidence_id).where(
            work_evidence.c.tenant_id == claim.tenant_id,
            work_evidence.c.work_id == claim.work_id,
            work_evidence.c.evidence_id.in_(claim.evidence_ids),
        )
    ).all()
    existing = {str(row.evidence_id) for row in rows}
    missing = tuple(evidence_id for evidence_id in claim.evidence_ids if evidence_id not in existing)
    if missing:
        raise WorkConflictError("claim references evidence outside the tenant-owned WorkItem")


def _find_active_work(
    connection: Connection,
    *,
    tenant_id: str,
    requester_id: str,
    digital_employee_id: str,
    playbook_id: str,
) -> WorkItem | None:
    row = (
        connection
        .execute(
            select(work_items)
            .where(
                work_items.c.tenant_id == tenant_id,
                work_items.c.requester_id == requester_id,
                work_items.c.digital_employee_id == digital_employee_id,
                work_items.c.playbook_id == playbook_id,
                work_items.c.status.not_in(_terminal_status_values()),
            )
            .order_by(work_items.c.updated_at.desc(), work_items.c.work_id.desc())
            .limit(1)
        )
        .mappings()
        .one_or_none()
    )
    return _row_to_item(row) if row is not None else None


def _require_owned_work(connection: Connection, *, tenant_id: str, work_id: str) -> str:
    requester_id = connection.execute(
        select(work_items.c.requester_id).where(
            work_items.c.tenant_id == tenant_id,
            work_items.c.work_id == work_id,
        )
    ).scalar_one_or_none()
    if requester_id is None:
        raise WorkNotFoundError(f"work item {work_id} was not found for tenant")
    return str(requester_id)


def _create_work_contract(
    connection: Connection,
    contract: WorkContractSnapshot,
) -> WorkContractSnapshot:
    _require_contract_work_binding(connection, contract)
    existing = _get_contract_version(connection, contract)
    if existing is not None:
        if existing == contract:
            return existing
        raise WorkContractConflictError("WorkItem contract version already exists with different values")
    if _find_current_contract(
        connection,
        tenant_id=contract.tenant_id,
        work_id=contract.work_id,
        contract_type=contract.contract_type,
        for_update=True,
    ) is not None:
        raise WorkContractConflictError("a current WorkItem contract already exists for this type")
    connection.execute(insert(work_contracts).values(**_contract_values(contract)))
    return contract


def _revise_work_contract(
    connection: Connection,
    contract: WorkContractSnapshot,
) -> WorkContractSnapshot:
    _require_contract_work_binding(connection, contract)
    existing = _get_contract_version(connection, contract)
    if existing is not None:
        if existing == contract:
            return existing
        raise WorkContractConflictError("WorkItem contract version already exists with different values")
    current = _find_current_contract(
        connection,
        tenant_id=contract.tenant_id,
        work_id=contract.work_id,
        contract_type=contract.contract_type,
        for_update=True,
    )
    if current is None:
        raise WorkNotFoundError("current WorkItem contract was not found for tenant")
    if contract.contract_version != current.contract_version + 1:
        raise WorkContractConflictError("revised WorkItem contract must use the next version")
    latest_timestamp = current.confirmed_at or current.created_at
    if contract.created_at < latest_timestamp:
        raise WorkContractConflictError("revised WorkItem contract predates its current version")
    superseded = replace(
        current,
        status=WorkContractStatus.SUPERSEDED,
        superseded_at=contract.created_at,
    )
    updated = connection.execute(
        update(work_contracts)
        .where(
            work_contracts.c.work_id == current.work_id,
            work_contracts.c.contract_type == current.contract_type,
            work_contracts.c.contract_version == current.contract_version,
            work_contracts.c.status == current.status.value,
        )
        .values(**_contract_lifecycle_values(superseded))
    )
    if updated.rowcount != 1:
        raise WorkContractConflictError("WorkItem contract revision lost a concurrent update")
    connection.execute(insert(work_contracts).values(**_contract_values(contract)))
    return contract


def _require_contract_work_binding(connection: Connection, contract: WorkContractSnapshot) -> None:
    requester_id = _require_owned_work(
        connection,
        tenant_id=contract.tenant_id,
        work_id=contract.work_id,
    )
    if contract.created_by != requester_id:
        raise WorkContractConflictError("WorkItem contract creator must be the requester")


def _get_contract_version(
    connection: Connection,
    contract: WorkContractSnapshot,
) -> WorkContractSnapshot | None:
    row = (
        connection
        .execute(
            select(work_contracts).where(
                work_contracts.c.tenant_id == contract.tenant_id,
                work_contracts.c.work_id == contract.work_id,
                work_contracts.c.contract_type == contract.contract_type,
                work_contracts.c.contract_version == contract.contract_version,
            )
        )
        .mappings()
        .one_or_none()
    )
    return _row_to_contract(row) if row is not None else None


def _find_current_contract(
    connection: Connection,
    *,
    tenant_id: str,
    work_id: str,
    contract_type: str,
    for_update: bool = False,
) -> WorkContractSnapshot | None:
    query = select(work_contracts).where(
        work_contracts.c.tenant_id == tenant_id,
        work_contracts.c.work_id == work_id,
        work_contracts.c.contract_type == contract_type,
        work_contracts.c.status != WorkContractStatus.SUPERSEDED.value,
    )
    if for_update:
        query = query.with_for_update()
    row = connection.execute(query).mappings().one_or_none()
    return _row_to_contract(row) if row is not None else None


def _locked_budget_usage(
    connection: Connection,
    *,
    item: WorkItem,
    now: datetime,
) -> BudgetUsage:
    row = (
        connection
        .execute(select(work_budget_usage).where(work_budget_usage.c.work_id == item.work_id).with_for_update())
        .mappings()
        .one_or_none()
    )
    if row is not None:
        return _row_to_budget_usage(row)
    connection.execute(
        insert(work_budget_usage).values(
            work_id=item.work_id,
            tenant_id=item.tenant_id,
            **_usage_update_values(BudgetAmount(), BudgetAmount(), now),
        )
    )
    return BudgetUsage()


def _require_pack_snapshot_binding(connection: Connection, item: WorkItem) -> None:
    row = (
        connection
        .execute(
            select(pack_snapshots.c.pack_id, pack_snapshots.c.pack_version).where(
                pack_snapshots.c.pack_snapshot_id == item.pack_snapshot_id
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise WorkConflictError("WorkItem references an unregistered pack snapshot")
    if str(row["pack_id"]) != item.pack_id or str(row["pack_version"]) != item.pack_version:
        raise WorkConflictError("WorkItem pack id/version does not match its pack snapshot")


def _forfeit_active_reservations(connection: Connection, *, item: WorkItem, now: datetime) -> None:
    rows = (
        connection
        .execute(
            select(work_budget_reservations)
            .where(
                work_budget_reservations.c.work_id == item.work_id,
                work_budget_reservations.c.status == BudgetReservationStatus.ACTIVE.value,
            )
            .with_for_update()
        )
        .mappings()
        .all()
    )
    if not rows:
        return
    usage = _locked_budget_usage(connection, item=item, now=now)
    forfeited = BudgetAmount()
    for row in rows:
        reservation = _row_to_budget_reservation(row)
        forfeited = _add_amount(forfeited, reservation.reserved)
        connection.execute(
            update(work_budget_reservations)
            .where(work_budget_reservations.c.reservation_id == reservation.reservation_id)
            .values(
                status=BudgetReservationStatus.FORFEITED.value,
                actual_model_calls=reservation.reserved.model_calls,
                actual_input_tokens=reservation.reserved.input_tokens,
                actual_output_tokens=reservation.reserved.output_tokens,
                actual_external_queries=reservation.reserved.external_queries,
                finalized_at=now,
            )
        )
    connection.execute(
        update(work_budget_usage)
        .where(work_budget_usage.c.work_id == item.work_id)
        .values(
            **_usage_update_values(
                _add_amount(usage.used, forfeited),
                _subtract_amount(usage.reserved, forfeited),
                now,
            )
        )
    )


def _require_active_worker(item: WorkItem, *, worker_id: str, now: datetime) -> None:
    if (
        item.status is not WorkStatus.RUNNING
        or item.lease_owner != worker_id
        or item.lease_expires_at is None
        or item.lease_expires_at <= now
    ):
        _raise_worker_lease_required()


def _require_budget_capacity(
    *,
    usage: BudgetUsage,
    requested: BudgetAmount,
    budget_row: RowMapping | Mapping[str, Any],
) -> None:
    allocated = _add_amount(_add_amount(usage.used, usage.reserved), requested)
    limits = BudgetAmount(
        model_calls=int(budget_row["max_model_calls"]),
        input_tokens=int(budget_row["max_input_tokens"]),
        output_tokens=int(budget_row["max_output_tokens"]),
        external_queries=int(budget_row["max_external_queries"]),
    )
    exceeded = [
        field_name
        for field_name in ("model_calls", "input_tokens", "output_tokens", "external_queries")
        if getattr(allocated, field_name) > getattr(limits, field_name)
    ]
    if exceeded:
        raise BudgetExceededError(f"budget exhausted for: {', '.join(exceeded)}")


def _add_amount(left: BudgetAmount, right: BudgetAmount) -> BudgetAmount:
    return BudgetAmount(
        model_calls=left.model_calls + right.model_calls,
        input_tokens=left.input_tokens + right.input_tokens,
        output_tokens=left.output_tokens + right.output_tokens,
        external_queries=left.external_queries + right.external_queries,
    )


def _subtract_amount(left: BudgetAmount, right: BudgetAmount) -> BudgetAmount:
    try:
        return BudgetAmount(
            model_calls=left.model_calls - right.model_calls,
            input_tokens=left.input_tokens - right.input_tokens,
            output_tokens=left.output_tokens - right.output_tokens,
            external_queries=left.external_queries - right.external_queries,
        )
    except ValueError as exc:
        raise BudgetReservationError("budget usage counters are inconsistent") from exc


def _amount_within(actual: BudgetAmount, ceiling: BudgetAmount) -> bool:
    return all(
        getattr(actual, field_name) <= getattr(ceiling, field_name)
        for field_name in ("model_calls", "input_tokens", "output_tokens", "external_queries")
    )


def _usage_update_values(used: BudgetAmount, reserved: BudgetAmount, now: datetime) -> dict[str, Any]:
    return {
        "used_model_calls": used.model_calls,
        "used_input_tokens": used.input_tokens,
        "used_output_tokens": used.output_tokens,
        "used_external_queries": used.external_queries,
        "reserved_model_calls": reserved.model_calls,
        "reserved_input_tokens": reserved.input_tokens,
        "reserved_output_tokens": reserved.output_tokens,
        "reserved_external_queries": reserved.external_queries,
        "updated_at": now,
    }


def _row_to_budget_usage(row: RowMapping | Mapping[str, Any]) -> BudgetUsage:
    return BudgetUsage(
        used=BudgetAmount(
            model_calls=int(row["used_model_calls"]),
            input_tokens=int(row["used_input_tokens"]),
            output_tokens=int(row["used_output_tokens"]),
            external_queries=int(row["used_external_queries"]),
        ),
        reserved=BudgetAmount(
            model_calls=int(row["reserved_model_calls"]),
            input_tokens=int(row["reserved_input_tokens"]),
            output_tokens=int(row["reserved_output_tokens"]),
            external_queries=int(row["reserved_external_queries"]),
        ),
    )


def _budget_reservation_values(reservation: BudgetReservation) -> dict[str, Any]:
    return {
        "reservation_id": reservation.reservation_id,
        "work_id": reservation.work_id,
        "tenant_id": reservation.tenant_id,
        "worker_id": reservation.worker_id,
        "phase": reservation.phase,
        "phase_attempt": reservation.phase_attempt,
        "idempotency_key": reservation.idempotency_key,
        "status": reservation.status.value,
        "reserved_model_calls": reservation.reserved.model_calls,
        "reserved_input_tokens": reservation.reserved.input_tokens,
        "reserved_output_tokens": reservation.reserved.output_tokens,
        "reserved_external_queries": reservation.reserved.external_queries,
        "actual_model_calls": reservation.actual.model_calls,
        "actual_input_tokens": reservation.actual.input_tokens,
        "actual_output_tokens": reservation.actual.output_tokens,
        "actual_external_queries": reservation.actual.external_queries,
        "created_at": reservation.created_at,
        "finalized_at": reservation.finalized_at,
    }


def _row_to_budget_reservation(row: RowMapping | Mapping[str, Any]) -> BudgetReservation:
    return BudgetReservation(
        reservation_id=str(row["reservation_id"]),
        work_id=str(row["work_id"]),
        tenant_id=str(row["tenant_id"]),
        worker_id=str(row["worker_id"]),
        phase=str(row["phase"]),
        phase_attempt=int(row["phase_attempt"]),
        idempotency_key=str(row["idempotency_key"]),
        status=BudgetReservationStatus(str(row["status"])),
        reserved=BudgetAmount(
            model_calls=int(row["reserved_model_calls"]),
            input_tokens=int(row["reserved_input_tokens"]),
            output_tokens=int(row["reserved_output_tokens"]),
            external_queries=int(row["reserved_external_queries"]),
        ),
        actual=BudgetAmount(
            model_calls=int(row["actual_model_calls"]),
            input_tokens=int(row["actual_input_tokens"]),
            output_tokens=int(row["actual_output_tokens"]),
            external_queries=int(row["actual_external_queries"]),
        ),
        created_at=_aware_datetime(row["created_at"]),
        finalized_at=_optional_datetime(row["finalized_at"]),
    )


def _json_document(value: Any, field_name: str) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise NonJsonValueError(f"{field_name} must be JSON-compatible") from exc


def _pack_snapshot_values(snapshot: PackSnapshot) -> dict[str, Any]:
    return {
        "pack_snapshot_id": snapshot.pack_snapshot_id,
        "pack_id": snapshot.pack_id,
        "pack_version": snapshot.pack_version,
        "source_repository": snapshot.source_repository,
        "source_commit": snapshot.source_commit,
        "manifest_digest": snapshot.manifest_digest,
        "content_artifact_id": snapshot.content_artifact_id,
        "asset_version_refs": _json_document(list(snapshot.asset_version_refs), "asset_version_refs"),
        "created_at": snapshot.created_at,
    }


def _row_to_pack_snapshot(row: RowMapping | Mapping[str, Any]) -> PackSnapshot:
    return PackSnapshot(
        pack_snapshot_id=str(row["pack_snapshot_id"]),
        pack_id=str(row["pack_id"]),
        pack_version=str(row["pack_version"]),
        source_repository=_optional_text(row["source_repository"]),
        source_commit=_optional_text(row["source_commit"]),
        manifest_digest=str(row["manifest_digest"]),
        content_artifact_id=str(row["content_artifact_id"]),
        asset_version_refs=tuple(str(value) for value in row["asset_version_refs"]),
        created_at=_aware_datetime(row["created_at"]),
    )


def _contract_values(contract: WorkContractSnapshot) -> dict[str, Any]:
    return {
        "work_id": contract.work_id,
        "contract_type": contract.contract_type,
        "contract_version": contract.contract_version,
        "tenant_id": contract.tenant_id,
        "status": contract.status.value,
        "payload": _json_document(dict(contract.payload), "contract payload"),
        "created_by": contract.created_by,
        "created_at": contract.created_at,
        "confirmed_by": contract.confirmed_by,
        "confirmed_at": contract.confirmed_at,
        "superseded_at": contract.superseded_at,
    }


def _contract_lifecycle_values(contract: WorkContractSnapshot) -> dict[str, Any]:
    return {
        "status": contract.status.value,
        "confirmed_by": contract.confirmed_by,
        "confirmed_at": contract.confirmed_at,
        "superseded_at": contract.superseded_at,
    }


def _row_to_contract(row: RowMapping | Mapping[str, Any]) -> WorkContractSnapshot:
    return WorkContractSnapshot(
        work_id=str(row["work_id"]),
        tenant_id=str(row["tenant_id"]),
        contract_type=str(row["contract_type"]),
        contract_version=int(row["contract_version"]),
        status=WorkContractStatus(str(row["status"])),
        payload=dict(row["payload"]),
        created_by=str(row["created_by"]),
        created_at=_aware_datetime(row["created_at"]),
        confirmed_by=_optional_text(row["confirmed_by"]),
        confirmed_at=_optional_datetime(row["confirmed_at"]),
        superseded_at=_optional_datetime(row["superseded_at"]),
    )


def _source_values(source: SourceRecord) -> dict[str, Any]:
    return {
        "source_id": source.source_id,
        "work_id": source.work_id,
        "tenant_id": source.tenant_id,
        "source_type": source.source_type.value,
        "title": source.title,
        "publisher": source.publisher,
        "published_at": source.published_at,
        "retrieved_at": source.retrieved_at,
        "locator": source.locator,
        "uri_digest": source.uri_digest,
        "file_id": source.file_id,
        "confidentiality_level": source.confidentiality_level,
        "authority_level": source.authority_level,
        "allowed_uses": _json_document(list(source.allowed_uses), "source allowed_uses"),
        "content_hash": source.content_hash,
        "result_digest": source.result_digest,
        "snapshot_policy": source.snapshot_policy,
        "snapshot_status": source.snapshot_status.value,
        "snapshot_artifact_id": source.snapshot_artifact_id,
        "license_restriction": source.license_restriction,
        "retrieval_query_digest": source.retrieval_query_digest,
        "license_terms_ref": source.license_terms_ref,
        "excerpt_status": source.excerpt_status.value,
        "metadata": _json_document(dict(source.metadata), "source metadata"),
    }


def _row_to_source(row: RowMapping | Mapping[str, Any]) -> SourceRecord:
    return SourceRecord(
        source_id=str(row["source_id"]),
        work_id=str(row["work_id"]),
        tenant_id=str(row["tenant_id"]),
        source_type=SourceType(str(row["source_type"])),
        title=str(row["title"]),
        publisher=str(row["publisher"]),
        published_at=_optional_datetime(row["published_at"]),
        retrieved_at=_aware_datetime(row["retrieved_at"]),
        locator=_optional_text(row["locator"]),
        uri_digest=str(row["uri_digest"]),
        file_id=_optional_text(row["file_id"]),
        confidentiality_level=str(row["confidentiality_level"]),
        authority_level=str(row["authority_level"]),
        allowed_uses=tuple(str(value) for value in row["allowed_uses"]),
        content_hash=str(row["content_hash"]),
        result_digest=str(row["result_digest"]),
        snapshot_policy=str(row["snapshot_policy"]),
        snapshot_status=SnapshotStatus(str(row["snapshot_status"])),
        snapshot_artifact_id=_optional_text(row["snapshot_artifact_id"]),
        license_restriction=_optional_text(row["license_restriction"]),
        retrieval_query_digest=str(row["retrieval_query_digest"]),
        license_terms_ref=_optional_text(row["license_terms_ref"]),
        excerpt_status=ExcerptStatus(str(row["excerpt_status"])),
        metadata=dict(row["metadata"]),
    )


def _evidence_values(evidence: EvidenceRecord) -> dict[str, Any]:
    return {
        "evidence_id": evidence.evidence_id,
        "work_id": evidence.work_id,
        "tenant_id": evidence.tenant_id,
        "source_id": evidence.source_id,
        "locator": evidence.locator,
        "excerpt": evidence.excerpt,
        "structured_value": (
            None
            if evidence.structured_value is None
            else _json_document(dict(evidence.structured_value), "evidence structured_value")
        ),
        "unit": evidence.unit,
        "period": evidence.period,
        "confidence": evidence.confidence,
        "extraction_method": evidence.extraction_method,
        "created_at": evidence.created_at,
        "metadata": _json_document(dict(evidence.metadata), "evidence metadata"),
    }


def _row_to_evidence(row: RowMapping | Mapping[str, Any]) -> EvidenceRecord:
    structured = row["structured_value"]
    return EvidenceRecord(
        evidence_id=str(row["evidence_id"]),
        work_id=str(row["work_id"]),
        tenant_id=str(row["tenant_id"]),
        source_id=str(row["source_id"]),
        locator=str(row["locator"]),
        excerpt=_optional_text(row["excerpt"]),
        structured_value=None if structured is None else dict(structured),
        unit=_optional_text(row["unit"]),
        period=_optional_text(row["period"]),
        confidence=float(row["confidence"]),
        extraction_method=str(row["extraction_method"]),
        created_at=_aware_datetime(row["created_at"]),
        metadata=dict(row["metadata"]),
    )


def _claim_values(claim: ClaimRecord) -> dict[str, Any]:
    return {
        "claim_id": claim.claim_id,
        "work_id": claim.work_id,
        "tenant_id": claim.tenant_id,
        "section_id": claim.section_id,
        "statement": claim.statement,
        "claim_type": claim.claim_type.value,
        "verification_status": claim.verification_status.value,
        "reviewer_status": claim.reviewer_status.value,
        "created_at": claim.created_at,
        "metadata": _json_document(dict(claim.metadata), "claim metadata"),
    }


def _row_to_claim(connection: Connection, row: RowMapping | Mapping[str, Any]) -> ClaimRecord:
    evidence_ids = tuple(
        str(binding.evidence_id)
        for binding in connection.execute(
            select(work_claim_evidence.c.evidence_id)
            .where(work_claim_evidence.c.claim_id == str(row["claim_id"]))
            .order_by(work_claim_evidence.c.ordinal)
        )
    )
    return ClaimRecord(
        claim_id=str(row["claim_id"]),
        work_id=str(row["work_id"]),
        tenant_id=str(row["tenant_id"]),
        section_id=str(row["section_id"]),
        statement=str(row["statement"]),
        claim_type=ClaimType(str(row["claim_type"])),
        evidence_ids=evidence_ids,
        verification_status=ClaimVerificationStatus(str(row["verification_status"])),
        reviewer_status=ClaimReviewerStatus(str(row["reviewer_status"])),
        created_at=_aware_datetime(row["created_at"]),
        metadata=dict(row["metadata"]),
    )


def _item_to_values(item: WorkItem) -> dict[str, Any]:
    return {
        "work_id": item.work_id,
        "tenant_id": item.tenant_id,
        "digital_employee_id": item.digital_employee_id,
        "digital_employee_profile_version": item.digital_employee_profile_version,
        "digital_employee_permissions_digest": item.digital_employee_permissions_digest,
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
        digital_employee_profile_version=_optional_text(row["digital_employee_profile_version"]),
        digital_employee_permissions_digest=_optional_text(row["digital_employee_permissions_digest"]),
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


def _raise_worker_lease_required() -> NoReturn:
    raise WorkConflictError("active worker lease is required to commit phase output")


def _validate_lease_request(worker_id: str, now: datetime, lease_duration: timedelta) -> None:
    if not worker_id.strip():
        raise ValueError("worker_id must not be blank")
    _require_aware_datetime(now, "now")
    if lease_duration <= timedelta(0):
        raise ValueError("lease_duration must be positive")


def _require_aware_datetime(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _system_transition(
    item: WorkItem,
    *,
    to_status: WorkStatus,
    event_id: str,
    event_type: str,
    actor_id: str,
    occurred_at: datetime,
) -> TransitionResult:
    from agentseek_work.state_machine import transition_work_item

    return transition_work_item(
        item,
        to_status=to_status,
        expected_version=item.version,
        event_id=event_id,
        event_type=event_type,
        actor_type=ActorType.SYSTEM,
        actor_id=actor_id,
        occurred_at=occurred_at,
        payload_digest="sha256:operational",
        policy_decision="allowed",
    )


def _operational_event(
    *,
    item: WorkItem,
    updated: WorkItem,
    event_id: str,
    event_type: str,
    actor_id: str,
    occurred_at: datetime,
) -> WorkEvent:
    return WorkEvent(
        event_id=event_id,
        work_id=item.work_id,
        event_type=event_type,
        actor_type=ActorType.SYSTEM,
        actor_id=actor_id,
        phase=updated.current_phase,
        from_status=item.status,
        to_status=updated.status,
        work_version=updated.version,
        payload_digest="sha256:operational",
        policy_decision="allowed",
        occurred_at=occurred_at,
    )


def _update_snapshot(connection: Connection, *, item: WorkItem, updated: WorkItem) -> None:
    values = _item_to_values(updated)
    values.pop("work_id")
    values.pop("tenant_id")
    result = connection.execute(
        update(work_items)
        .where(
            work_items.c.tenant_id == item.tenant_id,
            work_items.c.work_id == item.work_id,
            work_items.c.version == item.version,
        )
        .values(**values)
    )
    if result.rowcount != 1:
        _raise_optimistic_concurrency(item.work_id)
