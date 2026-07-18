from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from agentseek_work import WorkContractSnapshot, WorkContractStatus
from enterprise_wecom_digital_employee.report_brief import (
    REPORT_BRIEF_CONTRACT_TYPE,
    CoveragePeriodSource,
    ReportBrief,
    ResearchScope,
    explicitly_confirms_report_brief,
    validate_report_brief_scope,
)
from enterprise_wecom_digital_employee.work_tools import work_tools
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

NOW = datetime(2026, 7, 14, tzinfo=UTC)


class _GuidanceComposition:
    def current_work_summary(
        self,
        state: object,
        runtime_context: object | None = None,
    ) -> dict[str, object]:
        del state, runtime_context
        return {
            "work_id": "work_guidance",
            "status": "draft",
            "current_phase": "intake",
            "playbook_id": "securities-industry-report",
            "playbook_version": "1",
            "report_brief": {
                "contract_version": 3,
                "status": WorkContractStatus.PROVISIONAL.value,
            },
            "report_outline": {
                "contract_version": 2,
                "status": WorkContractStatus.PROVISIONAL.value,
                "report_brief_version": 3,
                "unresolved_question_count": 1,
            },
        }

    def save_report_brief(
        self,
        state: object,
        runtime_context: object | None,
        brief: ReportBrief,
    ) -> WorkContractSnapshot:
        del state, runtime_context
        return brief.to_contract(
            work_id="work_guidance",
            tenant_id="tenant_guidance",
            contract_version=3,
            created_by="employee_guidance",
            created_at=NOW,
        )


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
    assert contract.payload["schema_version"] == 2
    assert contract.payload["research_scope"] == "securities_industry"
    assert ReportBrief.from_contract(contract) == brief


def test_report_brief_reads_legacy_schema_one_as_securities_industry() -> None:
    contract = ReportBrief(title="证券行业报告", target_audience=("管理层",)).to_contract(
        work_id="work_legacy",
        tenant_id="tenant_001",
        contract_version=1,
        created_by="employee_001",
        created_at=NOW,
    )
    legacy = replace(
        contract,
        payload={
            key: value
            for key, value in contract.payload.items()
            if key != "research_scope"
        }
        | {"schema_version": 1},
    )

    assert ReportBrief.from_contract(legacy).research_scope is ResearchScope.SECURITIES_INDUSTRY


def test_report_brief_scope_is_deterministic_and_fails_closed() -> None:
    scopes = tuple(scope.value for scope in ResearchScope)
    anchors = ("证券", "券商", "资本市场")
    validate_report_brief_scope(
        ReportBrief(
            title="美联储政策对国内证券行业的影响",
            research_scope=ResearchScope.EXTERNAL_FACTOR_ON_SECURITIES,
        ),
        allowed_scopes=scopes,
        topic_anchor_terms=anchors,
    )

    with pytest.raises(ValueError, match="SCOPE_MISMATCH"):
        validate_report_brief_scope(
            ReportBrief(title="新能源汽车行业研究报告"),
            allowed_scopes=scopes,
            topic_anchor_terms=anchors,
        )


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


def test_save_report_brief_tool_constrains_formats_and_isolates_invalid_values() -> None:
    save_tool = cast(
        StructuredTool,
        {item.name: item for item in work_tools(cast(Any, object()))}["save_report_brief"],
    )
    schema_model = cast(type[BaseModel], save_tool.tool_call_schema)
    schema = schema_model.model_json_schema()
    format_variants = schema["properties"]["output_formats"]["anyOf"]
    array_schema = next(item for item in format_variants if item.get("type") == "array")

    assert array_schema["items"]["enum"] == ["markdown", "docx", "pdf"]
    assert schema["$defs"]["ResearchScope"]["enum"] == [scope.value for scope in ResearchScope]
    assert "Summary, report, and outline describe content" in save_tool.description

    save_func = save_tool.func
    assert save_func is not None
    result = save_func(
        title="证券行业摘要",
        target_audience=["管理层"],
        runtime=cast(Any, object()),
        output_formats=cast(Any, ["摘要"]),
    )

    assert "unsupported format" in result
    assert "markdown, docx, and pdf" in result


def test_work_tools_emit_unambiguous_contract_confirmation_guidance() -> None:
    tools = {
        item.name: cast(StructuredTool, item)
        for item in work_tools(cast(Any, _GuidanceComposition()))
    }
    runtime = cast(Any, SimpleNamespace(state={}, context=None))

    status_func = tools["get_current_work_status"].func
    assert status_func is not None
    status = status_func(runtime=runtime)

    assert "明确回复“确认 ReportBrief v3”" in status
    assert "明确回复“确认 ReportOutline v2”" in status
    assert status.count("不要只回复“确认 vN”") == 2

    save_func = tools["save_report_brief"].func
    assert save_func is not None
    saved = save_func(
        title="证券行业摘要",
        target_audience=["管理层"],
        runtime=runtime,
    )

    assert "明确回复“确认 ReportBrief v3”" in saved
    assert "不要只回复“确认 vN”" in saved
    assert "请员工确认上述版本" not in saved


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
    wrong_schema = replace(valid, payload={**valid.payload, "schema_version": 99})

    with pytest.raises(ValueError, match="not a report brief"):
        ReportBrief.from_contract(wrong_type)
    with pytest.raises(ValueError, match="unsupported report brief schema_version"):
        ReportBrief.from_contract(wrong_schema)


@pytest.mark.parametrize(
    "message",
    [
        "确认 ReportBrief v3，按这个版本开始。",
        "我同意 Report Brief version 3。",
        "批准报告简报第3版。",
    ],
)
def test_report_brief_confirmation_requires_explicit_exact_version(message: str) -> None:
    assert explicitly_confirms_report_brief(message, expected_version=3)


@pytest.mark.parametrize(
    "message",
    [
        "请立即启动内部知识检索。",
        "请按新的 ReportBrief 启动内部知识检索。",
        "确认 ReportBrief。",
        "确认 ReportBrief v2。",
        "不要确认 ReportBrief v3。",
        "请确认 ReportBrief v3。",
        "是否确认 ReportBrief v3？",
        "确认 ReportBrief v2 和 ReportBrief v3。",
    ],
)
def test_report_brief_confirmation_rejects_implicit_negated_or_ambiguous_messages(message: str) -> None:
    assert not explicitly_confirms_report_brief(message, expected_version=3)
