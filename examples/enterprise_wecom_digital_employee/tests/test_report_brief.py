from dataclasses import replace
from datetime import UTC, datetime

import pytest
from agentseek_work import WorkContractSnapshot, WorkContractStatus
from enterprise_wecom_digital_employee.report_brief import (
    REPORT_BRIEF_CONTRACT_TYPE,
    CoveragePeriodSource,
    ReportBrief,
)

NOW = datetime(2026, 7, 14, tzinfo=UTC)


def test_report_brief_is_progressive_and_uses_playbook_defaults() -> None:
    brief = ReportBrief(title="证券行业经营趋势报告")

    assert brief.coverage_period == "截至请求时间的最新可得数据"
    assert brief.coverage_period_source is CoveragePeriodSource.PLAYBOOK_DEFAULT
    assert brief.output_formats == ("docx",)
    assert brief.delivery_sla_minutes == 50
    assert brief.missing_fields == ("target_audience",)
    assert not brief.is_confirmable


def test_report_brief_becomes_confirmable_after_minimal_clarification() -> None:
    brief = ReportBrief(
        title="证券行业经营趋势报告",
        target_audience=("战略发展部",),
        coverage_period="2025年全年至2026年上半年",
        coverage_period_source=CoveragePeriodSource.EXPLICIT,
        output_formats=("docx", "pdf"),
    )

    assert brief.missing_fields == ()
    assert brief.is_confirmable


def test_report_brief_round_trips_through_generic_work_contract() -> None:
    brief = ReportBrief(
        title="证券行业经营趋势报告",
        target_audience=("公司管理层", "战略发展部"),
        coverage_period="最近三年",
        coverage_period_source=CoveragePeriodSource.INFERRED,
        confidentiality_level="confidential",
    )
    contract = brief.to_contract(
        work_id="work_001",
        tenant_id="tenant_001",
        contract_version=1,
        created_by="employee_001",
        created_at=NOW,
    )

    assert contract.contract_type == REPORT_BRIEF_CONTRACT_TYPE
    assert contract.status is WorkContractStatus.PROVISIONAL
    assert ReportBrief.from_contract(contract) == brief


@pytest.mark.parametrize(
    ("brief", "message"),
    [
        (ReportBrief(title="report"), "unsupported format"),
        (ReportBrief(title="report"), "confidentiality_level is unsupported"),
    ],
)
def test_report_brief_rejects_unsupported_contract_values(brief: ReportBrief, message: str) -> None:
    change = (
        {"output_formats": ("xlsx",)}
        if "format" in message
        else {"confidentiality_level": "secret"}
    )

    with pytest.raises(ValueError, match=message):
        replace(brief, **change)


def test_report_brief_rejects_duplicate_or_blank_audiences() -> None:
    with pytest.raises(ValueError, match="duplicates"):
        ReportBrief(title="report", target_audience=("管理层", "管理层"))
    with pytest.raises(ValueError, match="blank"):
        ReportBrief(title="report", target_audience=("",))


def test_report_brief_parser_fails_closed_on_wrong_contract_or_schema() -> None:
    valid = ReportBrief(title="report", target_audience=("管理层",)).to_contract(
        work_id="work_001",
        tenant_id="tenant_001",
        contract_version=1,
        created_by="employee_001",
        created_at=NOW,
    )
    wrong_type = WorkContractSnapshot(
        work_id=valid.work_id,
        tenant_id=valid.tenant_id,
        contract_type="travel-brief",
        contract_version=valid.contract_version,
        status=valid.status,
        payload=valid.payload,
        created_by=valid.created_by,
        created_at=valid.created_at,
    )
    wrong_schema = replace(valid, payload={**valid.payload, "schema_version": 2})

    with pytest.raises(ValueError, match="not a report brief"):
        ReportBrief.from_contract(wrong_type)
    with pytest.raises(ValueError, match="unsupported report brief schema_version"):
        ReportBrief.from_contract(wrong_schema)
