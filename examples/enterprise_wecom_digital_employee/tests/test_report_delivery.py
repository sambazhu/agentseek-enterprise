from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from agentseek_wecom.outbound import (
    register_template_card_action_handler,
    run_template_card_action,
    take_template_card_intent,
)
from agentseek_work import WorkConflictError
from enterprise_wecom_digital_employee.report_delivery import (
    REPORT_DELIVERY_CARD_ACTION_KIND,
    explicitly_requests_report_delivery,
    match_report_delivery_version,
)
from enterprise_wecom_digital_employee.work_tools import (
    _delivery_card_description,
    deliver_report_artifact_action,
    work_tools,
)
from langchain_core.tools import StructuredTool


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("交付 ReportArtifact v2 给我", True),
        ("交付 report artifact V2 给我。", True),
        (
            "from_userid=opaque|channel=$wecom|chat_id=opaque\n"
            "---Date: 2026-07-20T09:00:00+08:00---\n"
            "交付 ReportArtifact v2 给我",
            True,
        ),
        ("交付 ReportArtifact v1 给我", False),
        ("请交付 ReportArtifact v2 给我", False),
        ("交付 ReportArtifact v2 给张三", False),
        ("交付 ReportArtifact v2 给我并发布", False),
        ("不要交付 ReportArtifact v2 给我", False),
        ("交付 ReportArtifact v2 给我吗？", False),
        ("---Date: 2026-07-20T09:00:00+08:00---\n交付 ReportArtifact v2 给我", False),
    ],
)
def test_delivery_command_is_exact_and_wecom_envelope_aware(message: str, expected: bool) -> None:
    assert explicitly_requests_report_delivery(message, expected_version=2) is expected


def test_delivery_command_returns_exact_version_for_deterministic_dispatch() -> None:
    assert match_report_delivery_version("交付 ReportArtifact v12 给我") == 12
    assert match_report_delivery_version("请交付 ReportArtifact v12 给我") is None


class _ArtifactGuidanceComposition:
    def render_report_artifact(self, *_args: object, **_kwargs: object) -> object:
        return SimpleNamespace(
            artifact_id="artifact_current",
            source_contract_version=4,
            content_sha256="sha256:digest",
            size_bytes=1024,
        )

    def publish_report_artifact(self, *_args: object, **_kwargs: object) -> object:
        return SimpleNamespace(
            publication_id="publication_current",
            publication_version=2,
            artifact_id="artifact_current",
            source_contract_version=4,
            content_sha256="sha256:digest",
        )


def test_artifact_tools_emit_ledger_derived_next_step_versions() -> None:
    tools = {
        item.name: cast(StructuredTool, item)
        for item in work_tools(cast(Any, _ArtifactGuidanceComposition()))
    }
    runtime = cast(Any, SimpleNamespace(state={}, context=None))
    render_func = tools["render_report_docx_artifact"].func
    publish_func = tools["publish_report_artifact"].func
    assert render_func is not None
    assert publish_func is not None

    rendered = render_func(expected_version=4, runtime=runtime)
    published = publish_func(expected_version=4, runtime=runtime)

    assert "发布 ReportArtifact v4" in rendered
    assert "交付 ReportArtifact v4 给我" in published
    assert "原样转达" in rendered
    assert "原样转达" in published
    assert "ReportArtifact v1" not in rendered
    assert "ReportArtifact v1" not in published


class _ArtifactConflictComposition(_ArtifactGuidanceComposition):
    def render_report_artifact(self, *_args: object, **_kwargs: object) -> object:
        raise WorkConflictError("artifact ledger conflict")


def test_render_tool_contains_artifact_conflict_without_framework_traceback() -> None:
    tools = {
        item.name: cast(StructuredTool, item)
        for item in work_tools(cast(Any, _ArtifactConflictComposition()))
    }
    render_func = tools["render_report_docx_artifact"].func
    assert render_func is not None

    result = render_func(
        expected_version=4,
        runtime=cast(Any, SimpleNamespace(state={}, context=None)),
    )

    assert result == "artifact ledger conflict"


def test_delivery_card_explains_one_time_grant_and_reissue() -> None:
    description = _delivery_card_description(4)

    assert "本卡下载授权为一次性" in description
    assert "交付 ReportArtifact v4 给我" in description
    assert "获取新卡片" in description


def test_delivery_action_returns_only_opaque_marker_and_commits_after_card_send() -> None:
    committed: list[str] = []
    delivered_at = datetime(2026, 8, 31, tzinfo=UTC)
    commit_record = SimpleNamespace(
        delivery_id="delivery-1",
        delivery_version=1,
        work_id="work-1",
        tenant_id="tenant-1",
        artifact_id="artifact-1",
        publication_id="publication-1",
        content_sha256="sha256:" + "a" * 64,
        size_bytes=10,
        recipient_key="employee-1",
        grant_hash="sha256:" + "b" * 64,
        grant_expires_at=delivered_at + timedelta(hours=1),
        status=SimpleNamespace(value="delivered"),
        delivered_by="employee-1",
        delivered_at=delivered_at,
        grant_consumed_at=None,
        metadata={},
    )
    prepared = SimpleNamespace(
        already_delivered=False,
        filename="report-v4.docx",
        download_url="https://reports.example.test/download#token",
        record=SimpleNamespace(),
    )
    composition = SimpleNamespace(
        prepare_report_delivery=lambda *_args, **_kwargs: prepared,
        report_delivery_commit_record=lambda _value: commit_record,
    )
    register_template_card_action_handler(
        REPORT_DELIVERY_CARD_ACTION_KIND,
        lambda payload: committed.append(str(payload["delivery_id"])),
    )

    marker = deliver_report_artifact_action(
        composition=cast(Any, composition),
        state={},
        runtime_context=None,
        expected_version=4,
        latest_user_message="交付 ReportArtifact v4 给我",
    )

    assert marker.startswith("[[agentseek-wecom-template-card:")
    assert "受信" not in marker
    intent = take_template_card_intent(marker)
    assert intent is not None
    assert intent.success_action is not None
    run_template_card_action(intent.success_action)
    assert committed == ["delivery-1"]
