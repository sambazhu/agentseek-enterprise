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
        "report_artifacts": [{
            "artifact_id": "artifact_test",
            "format": "docx",
            "report_draft_version": 4,
            "current": True,
            "publication_status": "published",
            "delivery_status": "delivered",
            "storage_key": "/private/secret.docx",
        }],
        "report_publications": [{
            "publication_version": 2,
            "status": "published",
            "artifact_id": "artifact_test",
            "report_draft_version": 4,
            "current": True,
            "delivery_status": "delivered",
        }],
        "report_deliveries": [{
            "delivery_version": 3,
            "status": "delivered",
            "artifact_id": "artifact_test",
            "report_draft_version": 4,
            "current": True,
            "grant_state": "consumed",
            "grant_hash": "secret-token",
        }],
    }

    rendered = render_report_status(
        summary,
        sections=(
            ReportStatusSection.ARTIFACT,
            ReportStatusSection.PUBLICATION,
            ReportStatusSection.DELIVERY,
        ),
    )

    assert "artifact_id=artifact_test" in rendered
    assert "publication_v2" in rendered
    assert "delivery_v3" in rendered
    assert "grant_state=consumed" in rendered
    assert "This must never be rendered" not in rendered
    assert "/private/secret.docx" not in rendered
    assert "secret-token" not in rendered
    assert "ReportDraft" not in rendered


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
