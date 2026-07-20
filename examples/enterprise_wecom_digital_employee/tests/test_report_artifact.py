from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from enterprise_wecom_digital_employee.report_artifact import (
    ContentAddressedArtifactStore,
    ReportArtifactError,
    artifact_id,
    explicitly_requests_report_artifact,
    render_report_docx,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = (
    PROJECT_ROOT
    / "digital_employees"
    / "industry-report"
    / "assets"
    / "neutral-industry-report-v1.docx"
)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("生成 ReportDraft v1 DOCX", True),
        ("请导出 ReportDraft v1 Word 文档", True),
        ("生成 ReportDraft v2 DOCX", False),
        ("不要生成 ReportDraft v1 DOCX", False),
        ("是否生成 ReportDraft v1 DOCX？", False),
        ("生成 DOCX", False),
        ("批准 ReportDraft v1", False),
        ("生成 ReportDraft v1 PDF", False),
    ],
)
def test_explicit_docx_request_is_exact_and_fail_closed(message: str, expected: bool) -> None:
    assert explicitly_requests_report_artifact(
        message,
        expected_version=1,
        artifact_format="docx",
    ) is expected


def test_docx_render_is_deterministic_and_contains_approved_markdown(tmp_path: Path) -> None:
    template = TEMPLATE_PATH.read_bytes()
    markdown = "# 证券行业报告\n\n## 执行摘要\n\n- 结论一 [source_1]\n- 风险提示"

    first = render_report_docx(markdown=markdown, template_bytes=template)
    second = render_report_docx(markdown=markdown, template_bytes=template)

    assert first == second
    output = tmp_path / "report.docx"
    output.write_bytes(first)
    assert zipfile.is_zipfile(output)
    with zipfile.ZipFile(output) as document:
        xml = document.read("word/document.xml").decode()
        assert "证券行业报告" in xml
        assert "执行摘要" in xml
        assert "结论一 [source_1]" in xml
        assert "2025年中国证券行业" not in xml
        assert '<w:headerReference w:type="default" r:id="rId9"/>' in xml
        assert '<w:footerReference w:type="default" r:id="rId10"/>' in xml


def test_content_addressed_store_is_idempotent_and_never_exposes_absolute_path(tmp_path: Path) -> None:
    store = ContentAddressedArtifactStore(tmp_path)

    first = store.put(tenant_id="tenant-1", work_id="work_1", data=b"docx-bytes", suffix="docx")
    second = store.put(tenant_id="tenant-1", work_id="work_1", data=b"docx-bytes", suffix="docx")

    assert first == second
    assert first.storage_key.endswith(".docx")
    assert not first.storage_key.startswith("/")
    assert store.resolve(first.storage_key).read_bytes() == b"docx-bytes"
    with pytest.raises(ReportArtifactError, match="not safe"):
        store.put(tenant_id="../tenant", work_id="work_1", data=b"bad", suffix="docx")


def test_artifact_id_versions_the_approval_binding_while_blob_hash_stays_stable() -> None:
    values = {
        "tenant_id": "tenant-1",
        "work_id": "work_1",
        "content_sha256": f"sha256:{'a' * 64}",
        "source_digest": f"sha256:{'b' * 64}",
        "template_digest": f"sha256:{'c' * 64}",
    }

    first = artifact_id(**values, approval_digest=f"sha256:{'d' * 64}")
    second = artifact_id(**values, approval_digest=f"sha256:{'e' * 64}")

    assert first != second
