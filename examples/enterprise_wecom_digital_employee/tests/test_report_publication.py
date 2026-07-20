from __future__ import annotations

import pytest
from enterprise_wecom_digital_employee.report_publication import (
    explicitly_requests_report_publication,
    publication_id,
)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("发布 ReportArtifact v1", True),
        ("发布 report artifact V1。", True),
        ("发布 ReportArtifact v2", False),
        ("请发布 ReportArtifact v1", False),
        ("不要发布 ReportArtifact v1", False),
        ("是否发布 ReportArtifact v1？", False),
        ("发布 ReportDraft v1", False),
        ("发布 ReportArtifact", False),
        ("确认", False),
    ],
)
def test_publication_request_requires_exact_action_and_version(message: str, expected: bool) -> None:
    assert explicitly_requests_report_publication(message, expected_version=1) is expected


def test_publication_id_is_content_and_artifact_bound() -> None:
    values = {
        "tenant_id": "tenant-1",
        "work_id": "work-1",
        "content_sha256": f"sha256:{'a' * 64}",
    }

    first = publication_id(**values, artifact_id="artifact-1")
    replay = publication_id(**values, artifact_id="artifact-1")
    changed = publication_id(**values, artifact_id="artifact-2")

    assert first == replay
    assert first != changed
