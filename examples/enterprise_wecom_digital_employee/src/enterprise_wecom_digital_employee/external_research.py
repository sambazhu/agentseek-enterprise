from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from agentseek_work import (
    ExcerptStatus,
    SnapshotStatus,
    SourceRecord,
    SourceType,
    WorkConflictError,
)

from enterprise_wecom_digital_employee.report_research import (
    InternalResearchResult,
    build_research_query,
    research_question_map,
    run_internal_research,
)
from enterprise_wecom_digital_employee.research_gap_decision import (
    ResearchGapAction,
    ResearchGapDecision,
    gap_digest,
    message_digest,
)
from enterprise_wecom_digital_employee.work_composition import (
    IndustryReportWorkComposition,
    WorkCompositionError,
)

MCPInvoker = Callable[[str, str, dict[str, Any], bool], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class GapResolutionResult:
    internal: InternalResearchResult
    action: ResearchGapAction
    decision_contract_version: int
    sources: tuple[SourceRecord, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "work_id": self.internal.plan.work_id,
            "report_brief_version": self.internal.plan.report_brief_version,
            "research_plan_digest": self.internal.plan.digest,
            "gap_question_ids": list(self.internal.coverage.gaps),
            "action": self.action.value,
            "decision_contract_version": self.decision_contract_version,
            "external_search_used": self.action in {
                ResearchGapAction.GILDATA,
                ResearchGapAction.PUBLIC_WEB,
            },
            "source_ids": [source.source_id for source in self.sources],
            "next_step": _next_step(self.action),
        }


async def resolve_research_gaps(
    *,
    composition: IndustryReportWorkComposition,
    state: Mapping[str, object],
    runtime_context: object | None,
    template_path: Path,
    action: ResearchGapAction,
    latest_user_message: str,
    invoke_mcp: MCPInvoker,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> GapResolutionResult:
    # Refresh internal coverage in the same server-controlled call so an external
    # action can never bypass the department-knowledge-first contract.
    internal = await run_internal_research(
        composition=composition,
        state=state,
        runtime_context=runtime_context,
        template_path=template_path,
        invoke_mcp=invoke_mcp,
        clock=clock,
    )
    gaps = internal.coverage.gaps
    if not gaps:
        raise WorkCompositionError("当前 ReportBrief 的内部知识检索已覆盖全部研究问题。")
    decision = ResearchGapDecision(
        report_brief_version=internal.plan.report_brief_version,
        research_plan_digest=internal.plan.digest,
        gap_digest=gap_digest(gaps),
        gap_question_ids=gaps,
        action=action,
        authorization_message_digest=message_digest(latest_user_message),
    )
    contract = composition.confirm_research_gap_decision(
        state,
        runtime_context,
        decision=decision,
        latest_user_message=latest_user_message,
    )

    if action not in {ResearchGapAction.GILDATA, ResearchGapAction.PUBLIC_WEB}:
        return GapResolutionResult(internal, action, contract.contract_version, ())

    item = composition.current_work(state, runtime_context)
    if item is None:  # guarded by load_current_research_result
        raise WorkCompositionError("当前报告任务不可用。")
    existing = _existing_external_sources(
        composition=composition,
        tenant_id=item.tenant_id,
        work_id=item.work_id,
        decision_contract_version=contract.contract_version,
        action=action,
    )
    existing_by_question: dict[str, SourceRecord] = {}
    for source in existing:
        question_ids = source.metadata.get("question_ids")
        if isinstance(question_ids, list) and len(question_ids) == 1:
            existing_by_question[str(question_ids[0])] = source
    questions = research_question_map(internal.plan)
    sources: list[SourceRecord] = []
    for question_id in gaps:
        if question_id in existing_by_question:
            sources.append(existing_by_question[question_id])
            continue
        question = questions[question_id]
        query = build_research_query(internal.plan, question)
        server_name, tool_name, arguments = _external_call(action, query)
        try:
            raw = await invoke_mcp(server_name, tool_name, arguments, True)
        except Exception as exc:
            raise RuntimeError(f"{server_name}/{tool_name} execution failed") from exc
        _require_successful_result(raw, server_name=server_name, tool_name=tool_name)
        source = _external_source(
            work_id=item.work_id,
            tenant_id=item.tenant_id,
            report_brief_version=internal.plan.report_brief_version,
            research_plan_digest=internal.plan.digest,
            decision_contract_version=contract.contract_version,
            action=action,
            question_id=question_id,
            section_id=_section_id(internal, question_id),
            question_prompt=question.prompt,
            query=query,
            server_name=server_name,
            tool_name=tool_name,
            raw=raw,
            retrieved_at=clock(),
        )
        try:
            source = composition.repository.put_source_record(source)
        except WorkConflictError:
            source = composition.repository.get_source_record(
                tenant_id=item.tenant_id,
                source_id=source.source_id,
            )
        sources.append(source)
    return GapResolutionResult(internal, action, contract.contract_version, tuple(sources))


def gap_options(result: InternalResearchResult) -> dict[str, Any]:
    version = result.plan.report_brief_version
    choices = [] if not result.coverage.gaps else [
        {
            "action": ResearchGapAction.GILDATA.value,
            "confirmation": f"允许 ReportBrief v{version} 使用 Gildata 补充检索",
        },
        {
            "action": ResearchGapAction.PUBLIC_WEB.value,
            "confirmation": f"允许 ReportBrief v{version} 使用 Tavily 公开搜索",
        },
        {
            "action": ResearchGapAction.UPLOAD_MATERIALS.value,
            "confirmation": f"为 ReportBrief v{version} 上传补充材料",
        },
        {
            "action": ResearchGapAction.CONTINUE_WITH_GAPS.value,
            "confirmation": f"ReportBrief v{version} 保留缺口继续生成",
        },
    ]
    return {
        "work_id": result.plan.work_id,
        "report_brief_version": version,
        "research_plan_digest": result.plan.digest,
        "coverage": result.as_dict()["coverage"],
        "choices": choices,
    }


def _external_call(action: ResearchGapAction, query: str) -> tuple[str, str, dict[str, Any]]:
    if action is ResearchGapAction.GILDATA:
        return "gildata_datamap-data", "FinGeneralQuery", {"query": query}
    if action is ResearchGapAction.PUBLIC_WEB:
        return "tavily-search", "tavily_search", {
            "query": query,
            "max_results": 3,
            "search_depth": "basic",
        }
    raise ValueError("action does not use an external MCP")


def _require_successful_result(raw: str, *, server_name: str, tool_name: str) -> None:
    clean = raw.strip()
    if not clean:
        raise RuntimeError(f"{server_name}/{tool_name} returned empty content")
    if clean.startswith("MCP tool ") or clean.startswith("MCP server "):
        raise RuntimeError(clean)


def _external_source(
    *,
    work_id: str,
    tenant_id: str,
    report_brief_version: int,
    research_plan_digest: str,
    decision_contract_version: int,
    action: ResearchGapAction,
    question_id: str,
    section_id: str,
    question_prompt: str,
    query: str,
    server_name: str,
    tool_name: str,
    raw: str,
    retrieved_at: datetime,
) -> SourceRecord:
    identity = f"{work_id}:{decision_contract_version}:{action.value}:{question_id}"
    source_id = f"source_sha256_{sha256(identity.encode()).hexdigest()}"
    result_digest = _digest_text(raw)
    locator = f"mcp://{server_name}/{tool_name}#question={question_id}"
    is_gildata = action is ResearchGapAction.GILDATA
    return SourceRecord(
        source_id=source_id,
        work_id=work_id,
        tenant_id=tenant_id,
        source_type=SourceType.GILDATA if is_gildata else SourceType.PUBLIC_WEB,
        title=question_prompt,
        publisher="聚源数据" if is_gildata else "Tavily",
        retrieved_at=retrieved_at,
        locator=locator,
        uri_digest=_digest_text(locator),
        content_hash=result_digest,
        result_digest=result_digest,
        confidentiality_level="internal",
        authority_level="licensed_database" if is_gildata else "public_web",
        allowed_uses=("research", "citation"),
        snapshot_policy="reference_only",
        snapshot_status=SnapshotStatus.REFERENCED,
        retrieval_query_digest=_digest_text(query),
        excerpt_status=ExcerptStatus.NOT_REQUESTED,
        license_terms_ref=(
            "enterprise-license://gildata/v1" if is_gildata else "provider-terms://tavily/v1"
        ),
        metadata={
            "provider": server_name,
            "tool_name": tool_name,
            "section_ids": [section_id],
            "question_ids": [question_id],
            "research_plan_digest": research_plan_digest,
            "report_brief_version": report_brief_version,
            "gap_decision_contract_version": decision_contract_version,
            "gap_action": action.value,
            "raw_result_stored": False,
        },
    )


def _existing_external_sources(
    *,
    composition: IndustryReportWorkComposition,
    tenant_id: str,
    work_id: str,
    decision_contract_version: int,
    action: ResearchGapAction,
) -> tuple[SourceRecord, ...]:
    return tuple(
        source
        for source in composition.repository.list_source_records(tenant_id=tenant_id, work_id=work_id)
        if source.metadata.get("gap_decision_contract_version") == decision_contract_version
        and source.metadata.get("gap_action") == action.value
    )


def _section_id(result: InternalResearchResult, question_id: str) -> str:
    for section in result.plan.template.sections:
        if any(question.question_id == question_id for question in section.questions):
            return section.section_id
    raise ValueError(f"research question {question_id!r} does not belong to the current template")


def _next_step(action: ResearchGapAction) -> str:
    if action is ResearchGapAction.UPLOAD_MATERIALS:
        return "等待员工上传 request-scoped 补充材料；未自动发布到部门知识库。"
    if action is ResearchGapAction.CONTINUE_WITH_GAPS:
        return "保留当前缺口，后续质量门必须显示这些缺口。"
    return "已登记外部检索 SourceRecord；Evidence/Claim 提取留待 M3。"


def _digest_text(value: str) -> str:
    return f"sha256:{sha256(value.encode()).hexdigest()}"
