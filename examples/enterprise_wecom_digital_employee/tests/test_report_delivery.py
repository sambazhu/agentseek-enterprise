from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from enterprise_wecom_digital_employee.report_delivery import explicitly_requests_report_delivery
from enterprise_wecom_digital_employee.work_tools import _delivery_card_description, work_tools
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


def test_delivery_card_explains_one_time_grant_and_reissue() -> None:
    description = _delivery_card_description(4)

    assert "本卡下载授权为一次性" in description
    assert "交付 ReportArtifact v4 给我" in description
    assert "获取新卡片" in description
