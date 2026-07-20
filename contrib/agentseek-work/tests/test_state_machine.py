from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from agentseek_work.models import ActorType, WorkItem, WorkStatus
from agentseek_work.state_machine import (
    InvalidTransitionError,
    OptimisticConcurrencyError,
    transition_work_item,
)

NOW = datetime(2026, 7, 12, tzinfo=UTC)


def make_item(*, status: WorkStatus = WorkStatus.DRAFT, version: int = 0) -> WorkItem:
    return WorkItem(
        work_id="work_001",
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
        idempotency_key="tenant_001:request_001",
        created_at=NOW,
        updated_at=NOW,
        status=status,
        version=version,
    )


def transition(item: WorkItem, to_status: WorkStatus, *, expected_version: int | None = None):
    return transition_work_item(
        item,
        to_status=to_status,
        expected_version=item.version if expected_version is None else expected_version,
        event_id=f"event_{item.version + 1}",
        event_type="status_changed",
        actor_type=ActorType.SYSTEM,
        actor_id="worker_001",
        occurred_at=NOW + timedelta(seconds=item.version + 1),
        payload_digest="sha256:payload",
        policy_decision="allowed",
    )


def test_happy_path_produces_versioned_immutable_events() -> None:
    draft = make_item()
    queued = transition(draft, WorkStatus.QUEUED)
    running = transition(queued.item, WorkStatus.RUNNING)
    succeeded = transition(running.item, WorkStatus.SUCCEEDED)

    assert draft.status is WorkStatus.DRAFT
    assert succeeded.item.status is WorkStatus.SUCCEEDED
    assert succeeded.item.version == 3
    assert succeeded.item.phase_attempt == 1
    assert succeeded.item.is_terminal
    assert queued.event.from_status is WorkStatus.DRAFT
    assert queued.event.to_status is WorkStatus.QUEUED
    assert queued.event.work_version == 1


def test_draft_can_cross_the_explicit_publication_checkpoint() -> None:
    published = transition(make_item(), WorkStatus.PUBLISHED)

    assert published.item.status is WorkStatus.PUBLISHED
    assert published.item.version == 1
    assert not published.item.is_terminal


@pytest.mark.parametrize(
    ("status", "target"),
    [
        (WorkStatus.DRAFT, WorkStatus.RUNNING),
        (WorkStatus.WAITING_INPUT, WorkStatus.SUCCEEDED),
        (WorkStatus.SUCCEEDED, WorkStatus.QUEUED),
        (WorkStatus.FAILED, WorkStatus.RUNNING),
        (WorkStatus.CANCELLED, WorkStatus.QUEUED),
    ],
)
def test_invalid_edges_fail_closed(status: WorkStatus, target: WorkStatus) -> None:
    with pytest.raises(InvalidTransitionError):
        transition(make_item(status=status), target)


def test_stale_version_is_rejected_before_transition() -> None:
    item = make_item(version=4)

    with pytest.raises(OptimisticConcurrencyError, match="expected 3, actual 4"):
        transition(item, WorkStatus.QUEUED, expected_version=3)


def test_waiting_external_releases_to_queue_without_incrementing_attempt() -> None:
    item = replace(make_item(status=WorkStatus.WAITING_EXTERNAL, version=2), phase_attempt=1)

    result = transition(item, WorkStatus.QUEUED)

    assert result.item.phase_attempt == 1
    assert result.item.status is WorkStatus.QUEUED


def test_phase_can_change_only_when_entering_running() -> None:
    waiting = replace(make_item(status=WorkStatus.WAITING_REVIEW, version=2), current_phase="outline")

    with pytest.raises(InvalidTransitionError, match="only when entering running"):
        transition_work_item(
            waiting,
            to_status=WorkStatus.QUEUED,
            expected_version=2,
            event_id="event_003",
            event_type="review_completed",
            actor_type=ActorType.REQUESTER,
            actor_id="employee_001",
            occurred_at=NOW + timedelta(seconds=3),
            payload_digest="sha256:review",
            policy_decision="allowed",
            phase="drafting",
        )
