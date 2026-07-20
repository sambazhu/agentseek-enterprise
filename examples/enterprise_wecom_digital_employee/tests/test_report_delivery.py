from __future__ import annotations

import pytest
from enterprise_wecom_digital_employee.report_delivery import explicitly_requests_report_delivery


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
