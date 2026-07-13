from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from agentseek_work.models import PackSnapshot, WorkBudget, WorkItem, WorkStatus

NOW = datetime(2026, 7, 12, tzinfo=UTC)


def make_item() -> WorkItem:
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
    )


def test_work_item_defaults_to_non_terminal_draft() -> None:
    item = replace(make_item(), brief={"title": "2025年中国证券行业发展研究报告"})

    assert item.status is WorkStatus.DRAFT
    assert item.version == 0
    assert item.phase_attempt == 0
    assert item.brief["title"] == "2025年中国证券行业发展研究报告"
    assert not item.is_terminal


def test_work_item_copies_brief_into_read_only_mapping() -> None:
    brief = {"title": "before"}
    item = replace(make_item(), brief=brief)
    brief["title"] = "after"

    assert item.brief["title"] == "before"
    with pytest.raises(TypeError):
        cast(Any, item.brief)["title"] = "blocked"


def test_work_item_rejects_naive_timestamps() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(make_item(), created_at=datetime(2026, 7, 12), updated_at=datetime(2026, 7, 12))


def test_work_budget_rejects_phase_duration_above_work_duration() -> None:
    with pytest.raises(ValueError, match="must not exceed"):
        WorkBudget(
            max_model_calls=10,
            max_input_tokens=1000,
            max_output_tokens=1000,
            max_external_queries=10,
            max_phase_duration_seconds=301,
            max_work_duration_seconds=300,
            max_retry_count=2,
        )


def test_pack_snapshot_is_immutable_and_rejects_duplicate_assets() -> None:
    with pytest.raises(ValueError, match="duplicates"):
        PackSnapshot(
            pack_snapshot_id="pack_snapshot_sha256_abc",
            pack_id="industry-report",
            pack_version="1.0.0",
            manifest_digest="sha256:manifest",
            content_artifact_id="pack-content://sha256/content",
            asset_version_refs=("asset@1", "asset@1"),
            created_at=NOW,
        )
