from __future__ import annotations

import pytest
from enterprise_wecom_digital_employee.report_status import (
    ReportStatusSection,
    match_report_status_sections,
    render_report_status,
)


def test_status_intent_accepts_authenticated_wecom_envelope_and_combines_sections() -> None:
    message = (
        "from_userid=opaque|channel=$wecom|chat_id=opaque\n"
        "---Date: 2026-07-22---\n"
        "查看当前 ReportArtifact 和发布交付状态"
    )

    assert match_report_status_sections(message) == (
        ReportStatusSection.ARTIFACT,
        ReportStatusSection.PUBLICATION,
        ReportStatusSection.DELIVERY,
    )


@pytest.mark.parametrize(
    "message",
    [
        "发布 ReportArtifact v1",
        "交付 ReportArtifact v1 给我",
        "生成 ReportDraft v1 DOCX",
        "ReportArtifact 当前是什么",
        "---Date: 2026-07-22---\n查看当前 ReportArtifact",
    ],
)
def test_status_intent_does_not_capture_actions_or_unauthenticated_envelopes(message: str) -> None:
    assert match_report_status_sections(message) is None


def test_full_report_status_selects_all_ledger_sections() -> None:
    assert match_report_status_sections("请查看一下当前报告任务状态") == tuple(
        ReportStatusSection
    )


def test_status_renderer_returns_selected_ledger_facts_without_draft_body() -> None:
    summary = {
        "work_id": "work_test",
        "status": "delivered",
        "current_phase": "delivery",
        "playbook_id": "securities-industry-report",
        "playbook_version": "1",
        "report_draft": {
            "contract_version": 4,
            "status": "confirmed",
            "report_outline_version": 3,
            "quality_status": "reviewable",
            "claim_count": 9,
            "markdown": "# This must never be rendered",
        },
        "report_artifacts": [
            {
                "artifact_id": "artifact_test",
                "format": "docx",
                "report_draft_version": 4,
                "current": True,
                "publication_status": "published",
                "delivery_status": "delivered",
                "storage_key": "/private/secret.docx",
            },
            {
                "artifact_id": "artifact_old",
                "format": "docx",
                "report_draft_version": 3,
                "current": False,
            },
        ],
        "report_publications": [
            {
                "publication_version": 2,
                "status": "published",
                "artifact_id": "artifact_test",
                "report_draft_version": 4,
                "current": True,
                "delivery_status": "delivered",
            },
            {
                "publication_version": 1,
                "status": "published",
                "artifact_id": "artifact_old",
                "report_draft_version": 3,
                "current": False,
            },
        ],
        "report_deliveries": [
            {
                "delivery_version": 3,
                "status": "delivered",
                "artifact_id": "artifact_test",
                "report_draft_version": 4,
                "current": True,
                "grant_state": "consumed",
                "grant_hash": "secret-token",
            },
            {
                "delivery_version": 2,
                "status": "delivered",
                "artifact_id": "artifact_test",
                "report_draft_version": 4,
                "current": True,
                "grant_state": "expired",
            },
            {
                "delivery_version": 1,
                "status": "delivered",
                "artifact_id": "artifact_old",
                "report_draft_version": 3,
                "current": False,
                "grant_state": "consumed",
            },
        ],
    }

    rendered = render_report_status(
        summary,
        sections=(
            ReportStatusSection.ARTIFACT,
            ReportStatusSection.PUBLICATION,
            ReportStatusSection.DELIVERY,
        ),
    )

    assert "当前文件：ReportArtifact v4（DOCX），发布=已发布，交付=已交付" in rendered
    assert "当前发布：ReportPublication v2，绑定 ReportArtifact v4，已发布" in rendered
    assert "最近交付：ReportDelivery v3，绑定 ReportArtifact v4，已交付" in rendered
    assert "下载授权=已下载" in rendered
    assert "历史 ReportArtifact：1 个" in rendered
    assert "历史 ReportPublication：1 个" in rendered
    assert "历史 ReportDelivery：2 个" in rendered
    assert "artifact_test" not in rendered
    assert "artifact_old" not in rendered
    assert "This must never be rendered" not in rendered
    assert "/private/secret.docx" not in rendered
    assert "secret-token" not in rendered
    assert "ReportDraft" not in rendered


def test_status_renderer_labels_all_stale_records_as_history() -> None:
    summary = {
        "work_id": "work_test",
        "status": "draft",
        "current_phase": "intake",
        "playbook_id": "securities-industry-report",
        "playbook_version": "1",
        "report_artifacts": [{
            "artifact_id": "artifact_old",
            "format": "docx",
            "report_draft_version": 3,
            "current": False,
        }],
    }

    rendered = render_report_status(
        summary,
        sections=(ReportStatusSection.ARTIFACT,),
    )

    assert "当前没有有效的 ReportArtifact" in rendered
    assert "历史 ReportArtifact：1 个" in rendered
    assert "当前文件" not in rendered
    assert "artifact_old" not in rendered


def test_status_renderer_reports_absent_selected_section() -> None:
    summary = {
        "work_id": "work_test",
        "status": "draft",
        "current_phase": "intake",
        "playbook_id": "securities-industry-report",
        "playbook_version": "1",
    }

    assert "尚未形成 ReportArtifact" in render_report_status(
        summary,
        sections=(ReportStatusSection.ARTIFACT,),
    )
