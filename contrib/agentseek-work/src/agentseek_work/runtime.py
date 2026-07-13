from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from agentseek_work.models import WorkItem
from agentseek_work.repository import SQLAlchemyWorkRepository
from agentseek_work.state_machine import TransitionResult


@dataclass(frozen=True, slots=True)
class WorkRuntimeService:
    repository: SQLAlchemyWorkRepository
    worker_id: str
    lease_duration: timedelta

    def claim_next(self, *, now: datetime, tenant_id: str | None = None) -> WorkItem | None:
        return self.repository.claim_next(
            worker_id=self.worker_id,
            event_id=_event_id(),
            now=now,
            lease_duration=self.lease_duration,
            tenant_id=tenant_id,
        )

    def renew(self, item: WorkItem, *, now: datetime) -> WorkItem:
        return self.repository.renew_lease(
            tenant_id=item.tenant_id,
            work_id=item.work_id,
            worker_id=self.worker_id,
            now=now,
            lease_duration=self.lease_duration,
        )

    def commit(self, result: TransitionResult, *, expected_version: int, now: datetime) -> WorkItem:
        return self.repository.commit_worker_transition(
            tenant_id=result.item.tenant_id,
            worker_id=self.worker_id,
            expected_version=expected_version,
            now=now,
            result=result,
        )

    def abandon(self, item: WorkItem, *, now: datetime) -> bool:
        return self.repository.abandon_lease(
            tenant_id=item.tenant_id,
            work_id=item.work_id,
            worker_id=self.worker_id,
            now=now,
        )

    def recover_one(self, *, now: datetime, tenant_id: str | None = None) -> WorkItem | None:
        return self.repository.recover_expired_lease(
            worker_id=self.worker_id,
            event_id=_event_id(),
            now=now,
            lease_duration=self.lease_duration,
            tenant_id=tenant_id,
        )


def _event_id() -> str:
    return f"event_{uuid4().hex}"
