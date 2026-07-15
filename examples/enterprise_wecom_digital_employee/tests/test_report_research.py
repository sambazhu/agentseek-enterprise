from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from agentseek_work import (
    SourceType,
    SQLAlchemyWorkRepository,
    WorkContractStatus,
    apply_migrations,
)
from enterprise_wecom_digital_employee.external_research import (
    _external_call,
    gap_options,
    resolve_research_gaps,
)
from enterprise_wecom_digital_employee.pack_loader import (
    FilesystemPackSnapshotStore,
    RestrictedPackLoader,
    build_pack_snapshot,
)
from enterprise_wecom_digital_employee.report_brief import ReportBrief
from enterprise_wecom_digital_employee.report_research import (
    CoverageStatus,
    build_research_plan,
    load_current_research_result,
    load_research_template,
    run_internal_research,
)
from enterprise_wecom_digital_employee.research_gap_decision import (
    RESEARCH_GAP_DECISION_CONTRACT_TYPE,
    ResearchGapAction,
    explicitly_selects_gap_action,
)
from enterprise_wecom_digital_employee.work_composition import (
    IndustryReportWorkComposition,
    WorkCompositionError,
)
from enterprise_wecom_digital_employee.work_tools import _latest_user_message_text
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import create_engine

PROJECT_ROOT = Path(__file__).parents[1]
PACK_ROOT = PROJECT_ROOT / "digital_employees" / "industry-report"
TEMPLATE_PATH = (
    PACK_ROOT
    / "skills"
    / "report-intake"
    / "references"
    / "internal-research-template.yaml"
)
ASSET_REF = "trusted-asset://strategic-report-docx/1.0.0"
NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def test_research_template_has_stable_unique_sections_and_questions() -> None:
    template = load_research_template(TEMPLATE_PATH)

    assert template.template_id == "neutral-industry-report-internal-research"
    assert template.report_asset_ref == "strategic-report-docx@1.0.0"
    assert len(template.sections) == 5
    assert len({section.section_id for section in template.sections}) == 5
    assert all(question.minimum_fused_score == 0.02 for section in template.sections for question in section.questions)
    assert all(
        question.minimum_semantic_score == 0.7
        for section in template.sections
        for question in section.questions
    )
    assert template.digest.startswith("sha256:")


def test_research_plan_requires_confirmed_report_brief() -> None:
    brief = ReportBrief(title="证券行业报告", target_audience=("公司管理层",))
    provisional = brief.to_contract(
        work_id="work_001",
        tenant_id="tenant_001",
        contract_version=1,
        created_by="employee_001",
        created_at=NOW,
    )

    with pytest.raises(ValueError, match="confirmed ReportBrief"):
        build_research_plan(provisional, load_research_template(TEMPLATE_PATH))


def test_confirmation_guard_reads_latest_human_message_from_tool_runtime_state() -> None:
    runtime = SimpleNamespace(state={
        "messages": [
            HumanMessage(content="确认 ReportBrief v3，按这个版本开始。"),
            AIMessage(content="", tool_calls=[]),
        ]
    })

    assert _latest_user_message_text(runtime) == "确认 ReportBrief v3，按这个版本开始。"


def test_report_brief_revision_requires_exact_reconfirmation(tmp_path: Path) -> None:
    composition, state = _confirmed_composition(tmp_path)
    revised = composition.save_report_brief(
        state,
        None,
        ReportBrief(
            title="证券行业数字化转型报告（修订）",
            target_audience=("公司管理层",),
            coverage_period="2025年至2026年全年",
        ),
    )

    assert revised.contract_version == 2
    assert revised.status is WorkContractStatus.PROVISIONAL
    with pytest.raises(WorkCompositionError, match="版本不匹配"):
        composition.confirm_report_brief(
            state,
            None,
            expected_version=1,
            latest_user_message="确认 ReportBrief v1。",
        )
    with pytest.raises(WorkCompositionError, match="未显式确认"):
        composition.confirm_report_brief(
            state,
            None,
            expected_version=2,
            latest_user_message="请立即启动内部知识检索。",
        )
    current = composition.repository.get_current_work_contract(
        tenant_id=revised.tenant_id,
        work_id=revised.work_id,
        contract_type=revised.contract_type,
    )
    assert current is not None
    assert current.status is WorkContractStatus.PROVISIONAL
    confirmed = composition.confirm_report_brief(
        state,
        None,
        expected_version=2,
        latest_user_message="确认 ReportBrief v2。",
    )
    assert confirmed.status is WorkContractStatus.CONFIRMED


@pytest.mark.anyio
async def test_internal_research_is_knowledge_only_persists_sources_and_reports_gaps(tmp_path: Path) -> None:
    composition, state = _confirmed_composition(tmp_path)
    calls: list[tuple[str, str, dict[str, Any], bool]] = []

    async def invoke(server: str, tool_name: str, arguments: dict[str, Any], confirmed: bool) -> str:
        calls.append((server, tool_name, arguments, confirmed))
        if tool_name == "knowledge_read_chunks":
            return json.dumps({
                "chunks": [{
                    "chunk_id": "chunk-digital",
                    "document_id": "doc-digital",
                    "title": "证券行业数字化规划",
                    "heading": "总体目标",
                    "ordinal": 0,
                    "content": "证券行业数字化转型以客户服务、经营管理和风险控制能力提升为目标。",
                }]
            }, ensure_ascii=False)
        query = str(arguments["query"])
        if "目标、重点任务和实施路径" in query:
            hits = [{
                "document_id": "doc-digital",
                "chunk_id": "chunk-digital",
                "title": "证券行业数字化规划",
                "heading": "总体目标",
                "excerpt": "提升客户服务、经营管理和风险控制能力。",
                "score": 0.032258,
                "keyword_score": None,
                "semantic_score": 0.843804,
            }]
        else:
            hits = [{
                "document_id": "doc-unrelated",
                "chunk_id": "chunk-unrelated",
                "title": "无关材料",
                "heading": "其他",
                "excerpt": "与当前研究问题没有充分关联。",
                "score": 0.015,
                "keyword_score": None,
                "semantic_score": 0.664,
            }]
        return json.dumps({"hits": hits}, ensure_ascii=False)

    first = await run_internal_research(
        composition=composition,
        state=state,
        runtime_context=None,
        template_path=TEMPLATE_PATH,
        invoke_mcp=invoke,
        clock=lambda: NOW,
    )
    replay = await run_internal_research(
        composition=composition,
        state=state,
        runtime_context=None,
        template_path=TEMPLATE_PATH,
        invoke_mcp=invoke,
        clock=lambda: NOW,
    )

    assert first.plan.report_brief_version == 1
    assert first.coverage.covered_questions == 1
    assert first.coverage.total_questions == 5
    assert first.coverage.ratio == 0.2
    assert first.coverage.sections[1].status is CoverageStatus.COVERED
    assert len(first.coverage.gaps) == 4
    assert len(first.sources) == 1
    assert replay.sources == first.sources
    assert len(composition.repository.list_source_records(
        tenant_id="tenant-test",
        work_id="work_live_001",
    )) == 1
    assert {server for server, _, _, _ in calls} == {"department-knowledge"}
    assert {tool for _, tool, _, _ in calls} == {"knowledge_search", "knowledge_read_chunks"}
    assert all(not confirmed for _, _, _, confirmed in calls)
    read_arguments = [
        arguments
        for _, tool_name, arguments, _ in calls
        if tool_name == "knowledge_read_chunks"
    ]
    assert read_arguments == [
        {"chunk_ids": ["chunk-digital"]},
        {"chunk_ids": ["chunk-digital"]},
    ]
    search_queries = [
        str(arguments["query"])
        for _, tool_name, arguments, _ in calls
        if tool_name == "knowledge_search"
    ]
    assert all("证券行业数字化转型报告" in query for query in search_queries)
    assert all("2025年至2026年上半年" in query for query in search_queries)
    assert first.as_dict()["external_search_used"] is False


@pytest.mark.parametrize(
    ("message", "action"),
    [
        ("允许 ReportBrief v4 使用 Gildata 补充检索", ResearchGapAction.GILDATA),
        ("允许 ReportBrief v4 使用 Tavily 公开搜索", ResearchGapAction.PUBLIC_WEB),
        ("为 ReportBrief v4 上传补充材料", ResearchGapAction.UPLOAD_MATERIALS),
        ("ReportBrief v4 保留缺口继续生成", ResearchGapAction.CONTINUE_WITH_GAPS),
    ],
)
def test_gap_choice_requires_exact_version_and_one_action(
    message: str,
    action: ResearchGapAction,
) -> None:
    assert explicitly_selects_gap_action(message, expected_version=4, expected_action=action)
    assert not explicitly_selects_gap_action(message, expected_version=3, expected_action=action)


@pytest.mark.parametrize(
    "message",
    [
        "允许使用 Tavily 公开搜索",
        "允许 ReportBrief v4 使用 Gildata 或 Tavily 补充检索",
        "不要允许 ReportBrief v4 使用 Tavily 公开搜索",
        "ReportBrief v4 不使用 Tavily 公开搜索",
        "ReportBrief v4 是否使用 Tavily 公开搜索？",
        "确认",
    ],
)
def test_gap_choice_rejects_missing_version_ambiguity_negation_or_question(message: str) -> None:
    assert not explicitly_selects_gap_action(
        message,
        expected_version=4,
        expected_action=ResearchGapAction.PUBLIC_WEB,
    )


def test_gap_external_provider_calls_are_fixed_server_side() -> None:
    assert _external_call(ResearchGapAction.GILDATA, "query") == (
        "gildata_datamap-data",
        "FinGeneralQuery",
        {"query": "query"},
    )
    assert _external_call(ResearchGapAction.PUBLIC_WEB, "query") == (
        "tavily-search",
        "tavily_search",
        {"query": "query", "max_results": 3, "search_depth": "basic"},
    )


@pytest.mark.anyio
async def test_external_gap_search_requires_choice_persists_sources_and_replays_idempotently(
    tmp_path: Path,
) -> None:
    composition, state = _confirmed_composition(tmp_path)
    await _seed_one_internal_source(composition, state)
    before = load_current_research_result(
        composition=composition,
        state=state,
        runtime_context=None,
        template_path=TEMPLATE_PATH,
    )
    options = gap_options(before)
    assert before.coverage.gaps == (
        "executive-summary.core-trends",
        "operating-benchmark.roe-and-structure",
        "business-line-benchmark.five-lines",
        "action-recommendations.priorities",
    )
    assert options["choices"][1]["confirmation"] == "允许 ReportBrief v1 使用 Tavily 公开搜索"

    calls: list[tuple[str, str, dict[str, Any], bool]] = []

    async def invoke(server: str, tool_name: str, arguments: dict[str, Any], confirmed: bool) -> str:
        calls.append((server, tool_name, arguments, confirmed))
        if server == "department-knowledge":
            return _internal_mcp_response(tool_name, arguments)
        return json.dumps({"results": [{"title": "public result", "url": "https://example.test"}]})

    latest = "允许 ReportBrief v1 使用 Tavily 公开搜索"
    first = await resolve_research_gaps(
        composition=composition,
        state=state,
        runtime_context=None,
        template_path=TEMPLATE_PATH,
        action=ResearchGapAction.PUBLIC_WEB,
        latest_user_message=latest,
        invoke_mcp=invoke,
        clock=lambda: NOW,
    )
    replay = await resolve_research_gaps(
        composition=composition,
        state=state,
        runtime_context=None,
        template_path=TEMPLATE_PATH,
        action=ResearchGapAction.PUBLIC_WEB,
        latest_user_message=latest,
        invoke_mcp=invoke,
        clock=lambda: NOW,
    )

    external_calls = [call for call in calls if call[0] != "department-knowledge"]
    assert len(external_calls) == 4
    assert all(call[:2] == ("tavily-search", "tavily_search") for call in external_calls)
    assert all(call[2]["max_results"] == 3 for call in external_calls)
    assert all(call[2]["search_depth"] == "basic" for call in external_calls)
    assert all(call[3] is True for call in external_calls)
    assert len(first.sources) == 4
    assert replay.sources == first.sources
    assert all(source.source_type is SourceType.PUBLIC_WEB for source in first.sources)
    assert all(source.metadata["raw_result_stored"] is False for source in first.sources)
    assert all("example.test" not in json.dumps(dict(source.metadata)) for source in first.sources)
    decision = composition.repository.get_current_work_contract(
        tenant_id="tenant-test",
        work_id="work_live_001",
        contract_type=RESEARCH_GAP_DECISION_CONTRACT_TYPE,
    )
    assert decision is not None
    assert decision.status is WorkContractStatus.CONFIRMED
    assert decision.confirmed_by == f"hmac-{'2' * 64}"


@pytest.mark.anyio
async def test_upload_choice_records_decision_without_external_mcp(tmp_path: Path) -> None:
    composition, state = _confirmed_composition(tmp_path)
    await _seed_one_internal_source(composition, state)
    calls: list[tuple[str, str, dict[str, Any], bool]] = []

    async def invoke(server: str, tool_name: str, arguments: dict[str, Any], confirmed: bool) -> str:
        calls.append((server, tool_name, arguments, confirmed))
        if server == "department-knowledge":
            return _internal_mcp_response(tool_name, arguments)
        return "unexpected external call"

    result = await resolve_research_gaps(
        composition=composition,
        state=state,
        runtime_context=None,
        template_path=TEMPLATE_PATH,
        action=ResearchGapAction.UPLOAD_MATERIALS,
        latest_user_message="为 ReportBrief v1 上传补充材料",
        invoke_mcp=invoke,
        clock=lambda: NOW,
    )

    assert calls
    assert {call[0] for call in calls} == {"department-knowledge"}
    assert result.sources == ()
    assert result.as_dict()["external_search_used"] is False
    assert "request-scoped" in result.as_dict()["next_step"]


@pytest.mark.anyio
async def test_implicit_gap_request_fails_closed_without_contract_or_mcp(tmp_path: Path) -> None:
    composition, state = _confirmed_composition(tmp_path)
    await _seed_one_internal_source(composition, state)
    calls: list[tuple[str, str, dict[str, Any], bool]] = []

    async def invoke(server: str, tool_name: str, arguments: dict[str, Any], confirmed: bool) -> str:
        calls.append((server, tool_name, arguments, confirmed))
        if server == "department-knowledge":
            return _internal_mcp_response(tool_name, arguments)
        return "unexpected external call"

    with pytest.raises(WorkCompositionError, match="未对 ReportBrief v1 明确选择"):
        await resolve_research_gaps(
            composition=composition,
            state=state,
            runtime_context=None,
            template_path=TEMPLATE_PATH,
            action=ResearchGapAction.PUBLIC_WEB,
            latest_user_message="请立即用外部搜索补齐缺口",
            invoke_mcp=invoke,
            clock=lambda: NOW,
        )
    assert calls
    assert {call[0] for call in calls} == {"department-knowledge"}
    assert composition.repository.get_current_work_contract(
        tenant_id="tenant-test",
        work_id="work_live_001",
        contract_type=RESEARCH_GAP_DECISION_CONTRACT_TYPE,
    ) is None


async def _seed_one_internal_source(
    composition: IndustryReportWorkComposition,
    state: dict[str, Any],
) -> None:
    async def invoke(server: str, tool_name: str, arguments: dict[str, Any], confirmed: bool) -> str:
        del server, confirmed
        return _internal_mcp_response(tool_name, arguments)

    await run_internal_research(
        composition=composition,
        state=state,
        runtime_context=None,
        template_path=TEMPLATE_PATH,
        invoke_mcp=invoke,
        clock=lambda: NOW,
    )


def _internal_mcp_response(tool_name: str, arguments: dict[str, Any]) -> str:
    if tool_name == "knowledge_read_chunks":
        return json.dumps({
            "chunks": [{
                "chunk_id": "chunk-digital",
                "document_id": "doc-digital",
                "title": "证券行业数字化规划",
                "content": "数字化转型以客户服务、经营管理和风险控制能力提升为目标。",
            }]
        }, ensure_ascii=False)
    query = str(arguments["query"])
    hits = (
        [{
            "document_id": "doc-digital",
            "chunk_id": "chunk-digital",
            "title": "证券行业数字化规划",
            "score": 0.032258,
            "semantic_score": 0.843804,
        }]
        if "目标、重点任务和实施路径" in query
        else []
    )
    return json.dumps({"hits": hits}, ensure_ascii=False)


def _confirmed_composition(tmp_path: Path) -> tuple[IndustryReportWorkComposition, dict[str, Any]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    apply_migrations(engine)
    repository = SQLAlchemyWorkRepository(engine)

    def resolve_asset(artifact_ref: str) -> Path:
        assert artifact_ref == ASSET_REF
        return PACK_ROOT / "assets" / "neutral-industry-report-v1.docx"

    loaded = RestrictedPackLoader(
        pack_root=PACK_ROOT,
        allowed_entrypoint_package="enterprise_wecom_digital_employee",
        asset_resolver=resolve_asset,
    ).load()
    snapshot = build_pack_snapshot(
        loaded,
        store=FilesystemPackSnapshotStore(tmp_path / "snapshots"),
        created_at=NOW,
    )
    repository.put_pack_snapshot(snapshot)
    composition = IndustryReportWorkComposition(
        repository=repository,
        loaded_pack=loaded,
        pack_snapshot_id=snapshot.pack_snapshot_id,
        runtime_release="enterprise-wecom-v0.1.0-m2",
        clock=lambda: NOW,
        id_factory=lambda: "work_live_001",
    )
    state = _authorized_state()
    composition.enrich_state(_message(), "wecom:test", state)
    composition.create_report_work(state)
    contract = composition.save_report_brief(
        state,
        None,
        ReportBrief(
            title="证券行业数字化转型报告",
            target_audience=("公司管理层",),
            coverage_period="2025年至2026年上半年",
        ),
    )
    confirmed = composition.confirm_report_brief(
        state,
        None,
        expected_version=contract.contract_version,
        latest_user_message=f"确认 ReportBrief v{contract.contract_version}，按这个版本开始。",
    )
    assert confirmed.status is WorkContractStatus.CONFIRMED
    return composition, state


def _authorized_state() -> dict[str, Any]:
    return {
        "employee_context": {
            "name": "测试员工",
            "oa_account": "not-published",
            "dept_name": "战略发展部",
            "org_path_label": "总部/战略发展部",
        },
        "_langgraph_runtime_context": {
            "enterprise": {
                "version": "v1",
                "tenant_id": "tenant-test",
                "tenant_key": f"hmac-{'1' * 64}",
                "user_key": f"hmac-{'2' * 64}",
                "session_key": f"hmac-{'3' * 64}",
            }
        },
    }


def _message() -> Mapping[str, Any]:
    return {
        "content": "请创建证券行业数字化转型报告",
        "context": {"wecom": {"raw": {"msgid": "message-001"}}},
    }
