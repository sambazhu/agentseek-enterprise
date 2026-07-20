from __future__ import annotations

import json
from collections.abc import Mapping

from agentseek_work import (
    ActiveWorkConflictError,
    SourceRecord,
    SourceType,
    WorkContractStatus,
    WorkItem,
)
from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import ToolRuntime

from {{ cookiecutter.project_slug }}.external_research import (
    gap_options,
)
from {{ cookiecutter.project_slug }}.external_research import (
    resolve_research_gaps as _resolve_research_gaps,
)
from {{ cookiecutter.project_slug }}.report_approval import approval_state
from {{ cookiecutter.project_slug }}.report_brief import (
    CoveragePeriodSource,
    ReportBrief,
    ReportOutputFormat,
    ResearchScope,
)
from {{ cookiecutter.project_slug }}.report_draft import (
    REPORT_DRAFT_CONTRACT_TYPE,
    REPORT_DRAFT_MARKDOWN_BEGIN,
    REPORT_DRAFT_MARKDOWN_END,
    DraftClaimProposal,
    ReportDraft,
)
from {{ cookiecutter.project_slug }}.report_draft import (
    build_report_draft as _build_report_draft,
)
from {{ cookiecutter.project_slug }}.report_draft import (
    prepare_report_draft_context as _prepare_report_draft_context,
)
from {{ cookiecutter.project_slug }}.report_outline import (
    REPORT_OUTLINE_CONTRACT_TYPE,
    OutlineQuestion,
    OutlineSection,
    ReportOutline,
    source_set_digest,
)
from {{ cookiecutter.project_slug }}.report_research import (
    InternalResearchResult,
)
from {{ cookiecutter.project_slug }}.report_research import (
    load_current_research_result as _load_current_research_result,
)
from {{ cookiecutter.project_slug }}.report_research import (
    run_internal_research as _run_internal_research,
)
from {{ cookiecutter.project_slug }}.research_gap_decision import (
    RESEARCH_GAP_DECISION_CONTRACT_TYPE,
    ResearchGapAction,
    ResearchGapDecision,
    gap_digest,
)
from {{ cookiecutter.project_slug }}.tools import call_mcp_tool
from {{ cookiecutter.project_slug }}.work_composition import (
    IndustryReportWorkComposition,
    WorkCompositionError,
)


def work_tools(composition: IndustryReportWorkComposition) -> list[BaseTool]:  # noqa: C901
    """Return requester-scoped tools backed by the durable WorkItem ledger."""

    @tool("create_industry_report_work")
    def create_industry_report_work(runtime: ToolRuntime) -> str:
        """Create the tracked formal securities-industry report task requested in this turn.

        Use only when the employee explicitly asks to create, write, prepare, track,
        or audit a complete formal industry report. This server-side tool is fixed
        to work_mode=required. It cannot run as a DirectTurn and does not start
        research or invent a confirmed ReportBrief.
        """

        try:
            result = composition.create_report_work(runtime.state, runtime.context)
        except ActiveWorkConflictError as exc:
            item = exc.existing
            return (
                "当前已有进行中的同类报告任务："
                f"work_id={item.work_id}，status={item.status.value}，phase={item.current_phase}。"
                "请先继续、完成或取消该任务，再创建新的同类报告任务。"
            )
        except WorkCompositionError as exc:
            return str(exc)
        verb = "已创建" if result.created else "已找到同一幂等请求创建的"
        return (
            f"{verb}正式报告任务：work_id={result.item.work_id}，"
            f"status={result.item.status.value}，phase={result.item.current_phase}。"
            "任务已固定数字员工、PackSnapshot、Playbook、Skill digest、运行版本和角色映射。"
            "下一步需要形成并确认轻量 ReportBrief；研究尚未启动，请勿声称报告已经开始编写或完成。"
        )

    @tool("get_current_work_status")
    def get_current_work_status(runtime: ToolRuntime) -> str:  # noqa: C901
        """Read current WorkItem, ReportBrief, and gap-decision versions from the ledger."""

        summary = composition.current_work_summary(runtime.state, runtime.context)
        if summary is None:
            return "当前员工没有可见的进行中任务。"
        lines = [
            f"当前任务：work_id={summary['work_id']}，status={summary['status']}，"
            f"phase={summary['current_phase']}，"
            f"playbook={summary['playbook_id']}@{summary['playbook_version']}。"
        ]
        brief = summary.get("report_brief")
        if isinstance(brief, Mapping):
            lines.append(
                f"当前 ReportBrief：v{brief.get('contract_version')}，status={brief.get('status')}。"
            )
            if brief.get("status") == WorkContractStatus.PROVISIONAL.value:
                lines.append(
                    "如认可，请明确回复"
                    f"“确认 ReportBrief v{brief.get('contract_version')}”；不要只回复“确认 vN”。"
                )
        decision = summary.get("research_gap_decision")
        if isinstance(decision, Mapping):
            lines.append(
                "最新缺口决策："
                f"contract_v{decision.get('contract_version')}，"
                f"bound_report_brief_v{decision.get('report_brief_version')}，"
                f"status={decision.get('status')}，action={decision.get('action')}。"
            )
            lines.append("缺口决策绑定版本是历史决策字段，不代表当前 ReportBrief 版本。")
        outline = summary.get("report_outline")
        if isinstance(outline, Mapping):
            lines.append(
                "当前 ReportOutline："
                f"v{outline.get('contract_version')}，status={outline.get('status')}，"
                f"bound_report_brief_v{outline.get('report_brief_version')}，"
                f"unresolved={outline.get('unresolved_question_count')}。"
            )
            if outline.get("status") == WorkContractStatus.PROVISIONAL.value:
                lines.append(
                    "如认可，请明确回复"
                    f"“确认 ReportOutline v{outline.get('contract_version')}”；不要只回复“确认 vN”。"
                )
        draft = summary.get("report_draft")
        if isinstance(draft, Mapping):
            lines.append(
                "当前 ReportDraft："
                f"v{draft.get('contract_version')}，status={draft.get('status')}，"
                f"bound_report_outline_v{draft.get('report_outline_version')}，"
                f"quality={draft.get('quality_status')}，claims={draft.get('claim_count')}。"
            )
            if draft.get("status") == WorkContractStatus.PROVISIONAL.value:
                lines.append(
                    "如认可该初稿，请明确回复"
                    f"“确认 ReportDraft v{draft.get('contract_version')}”；该确认不等于最终批准。"
                )
            elif draft.get("status") == WorkContractStatus.CONFIRMED.value:
                lines.append(
                    "如需进入内容审批，请另行回复"
                    f"“提交 ReportDraft v{draft.get('contract_version')} 审批”。"
                )
        approval = summary.get("report_approval")
        if isinstance(approval, Mapping):
            lines.append(
                "当前 ReportApproval："
                f"contract_v{approval.get('contract_version')}，status={approval.get('status')}，"
                f"bound_report_draft_v{approval.get('report_draft_version')}，"
                f"current={str(bool(approval.get('current'))).lower()}。"
            )
            if approval.get("status") == "pending" and approval.get("current") is True:
                lines.append(
                    "审批人如批准该内容，请明确回复"
                    f"“批准 ReportDraft v{approval.get('report_draft_version')}”。"
                )
            elif approval.get("status") == "approved" and approval.get("current") is True:
                lines.append(
                    "如需生成文件，请另行回复"
                    f"“生成 ReportDraft v{approval.get('report_draft_version')} DOCX”。"
                )
        artifacts = summary.get("report_artifacts")
        if isinstance(artifacts, list):
            for artifact in artifacts:
                if not isinstance(artifact, Mapping):
                    continue
                lines.append(
                    "当前 ReportArtifact："
                    f"artifact_id={artifact.get('artifact_id')}，format={artifact.get('format')}，"
                    f"bound_report_draft_v{artifact.get('report_draft_version')}，"
                    f"current={str(bool(artifact.get('current'))).lower()}，"
                    f"publication={artifact.get('publication_status')}，"
                    f"delivery={artifact.get('delivery_status')}。"
                )
        publications = summary.get("report_publications")
        if isinstance(publications, list):
            for publication in publications:
                if not isinstance(publication, Mapping):
                    continue
                lines.append(
                    "当前 ReportPublication："
                    f"publication_v{publication.get('publication_version')}，"
                    f"status={publication.get('status')}，"
                    f"artifact_id={publication.get('artifact_id')}，"
                    f"bound_report_draft_v{publication.get('report_draft_version')}，"
                    f"current={str(bool(publication.get('current'))).lower()}，"
                    f"delivery={publication.get('delivery_status')}。"
                )
        return "\n".join(lines)

    @tool("save_report_brief")
    def save_report_brief(
        title: str,
        target_audience: list[str],
        runtime: ToolRuntime,
        coverage_period: str = "",
        output_formats: list[ReportOutputFormat] | None = None,
        confidentiality_level: str = "internal",
        research_scope: ResearchScope = ResearchScope.SECURITIES_INDUSTRY,
    ) -> str:
        """Save or revise the lightweight ReportBrief for the current report WorkItem.

        Use the employee's stated topic and audience. coverage_period may be empty,
        in which case the approved playbook default is used. Saving is provisional:
        it does not authorize research. Show the returned version and summary to the
        employee and ask them to confirm that exact version. output_formats accepts
        only markdown, docx, or pdf. Summary, report, and outline describe content,
        not file formats; omit output_formats to use the default docx format.
        You must call this tool for every save or revision. Never narrate that a
        ReportBrief was saved, revised, or reformatted unless this tool returns a
        successful ledger version in the same turn. research_scope must describe
        one securities role boundary: securities_industry, securities_company,
        securities_business_line, or external_factor_on_securities. A report about
        another industry is allowed only when its title explicitly states the impact
        on securities; otherwise ask for clarification and do not save the Brief.
        """

        try:
            clean_period = coverage_period.strip()
            brief = ReportBrief(
                title=title.strip(),
                research_scope=research_scope,
                target_audience=tuple(value.strip() for value in target_audience if value.strip()),
                coverage_period=clean_period or "截至请求时间的最新可得数据",
                coverage_period_source=(
                    CoveragePeriodSource.EXPLICIT if clean_period else CoveragePeriodSource.PLAYBOOK_DEFAULT
                ),
                output_formats=tuple(output_formats or ["docx"]),
                confidentiality_level=confidentiality_level,
            )
            contract = composition.save_report_brief(runtime.state, runtime.context, brief)
        except (ValueError, WorkCompositionError) as exc:
            return str(exc)
        return (
            f"ReportBrief v{contract.contract_version} 已保存，状态={contract.status.value}。"
            f"主题：{brief.title}；研究范围：{brief.research_scope.value}；"
            f"目标受众：{'、'.join(brief.target_audience)}；"
            f"报告覆盖期：{brief.coverage_period}；输出：{','.join(brief.output_formats)}。"
            f"如认可，请明确回复“确认 ReportBrief v{contract.contract_version}”；"
            "不要只回复“确认 vN”。未确认前不得启动正式知识检索。"
        )

    @tool("confirm_report_brief")
    def confirm_report_brief(expected_version: int, runtime: ToolRuntime) -> str:
        """Confirm the exact current ReportBrief version after explicit requester approval.

        When the latest employee message names a ReportBrief version with confirmation
        intent, call this tool with that version. Do not validate case, spacing, or
        spelling yourself; the server parser is the sole authority and fails closed.
        Never infer confirmation from earlier turns.
        """

        try:
            contract = composition.confirm_report_brief(
                runtime.state,
                runtime.context,
                expected_version=expected_version,
                latest_user_message=_latest_user_message_text(runtime),
            )
        except (ValueError, WorkCompositionError) as exc:
            return str(exc)
        return (
            f"ReportBrief v{contract.contract_version} 已由任务委派人确认。"
            "现在可以启动模板驱动的内部知识检索；尚未授权外部搜索或报告写作。"
        )

    @tool("run_internal_report_research")
    async def run_internal_report_research(runtime: ToolRuntime) -> str:
        """Run the approved-template internal knowledge-first retrieval plan.

        Requires a confirmed ReportBrief. This tool calls only the authorized
        department-knowledge MCP, reads selected chunks, registers immutable
        SourceRecords, and computes deterministic section coverage and gaps. It
        never calls Gildata, Tavily, or another external source.
        """

        async def invoke(server: str, tool_name: str, arguments: dict, confirmed: bool) -> str:
            return await call_mcp_tool(server, tool_name, arguments, confirmed)

        try:
            result = await _run_internal_research(
                composition=composition,
                state=runtime.state,
                runtime_context=runtime.context,
                template_path=composition.research_template_path,
                invoke_mcp=invoke,
            )
        except (RuntimeError, ValueError, WorkCompositionError) as exc:
            return str(exc)
        return json.dumps(result.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)

    @tool("get_report_research_gaps")
    def get_report_research_gaps(runtime: ToolRuntime) -> str:
        """Show deterministic internal-knowledge coverage gaps and exact employee choices.

        This is read-only. Present the returned confirmation phrase for one option
        verbatim. Do not infer authorization and do not call an external MCP yourself.
        """

        try:
            result = _load_current_research_result(
                composition=composition,
                state=runtime.state,
                runtime_context=runtime.context,
                template_path=composition.research_template_path,
            )
        except (RuntimeError, ValueError, WorkCompositionError) as exc:
            return str(exc)
        return json.dumps(gap_options(result), ensure_ascii=False, indent=2, sort_keys=True)

    @tool("resolve_report_research_gaps")
    async def resolve_report_research_gaps(action: ResearchGapAction, runtime: ToolRuntime) -> str:
        """Apply one explicitly selected action to the current ReportBrief gaps.

        The latest employee message must name the exact ReportBrief version and
        exactly one action: Gildata, Tavily public search, upload materials, or
        continue with gaps. The server verifies that message, persists a confirmed
        decision contract, and only then may execute the selected external MCP.
        Never call this tool based on an earlier turn or a generic "confirm". The
        current ReportBrief version comes from the latest confirmed report-brief
        contract, never from an older gap-decision contract.
        """

        async def invoke(server: str, tool_name: str, arguments: dict, confirmed: bool) -> str:
            return await call_mcp_tool(server, tool_name, arguments, confirmed)

        try:
            result = await _resolve_research_gaps(
                composition=composition,
                state=runtime.state,
                runtime_context=runtime.context,
                template_path=composition.research_template_path,
                action=action,
                latest_user_message=_latest_user_message_text(runtime),
                invoke_mcp=invoke,
            )
        except (RuntimeError, ValueError, WorkCompositionError) as exc:
            return str(exc)
        return json.dumps(result.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)

    @tool("build_report_outline")
    def build_report_outline(runtime: ToolRuntime) -> str:
        """Build and save a deterministic source-backed ReportOutline contract.

        Requires a confirmed current ReportBrief and completed internal research.
        If internal gaps remain, the employee must first confirm one exact current
        gap action. External-search decisions must have a SourceRecord for every
        gap; upload-materials remains waiting, while continue-with-gaps preserves
        unresolved question IDs. This tool stores only section/question/source
        bindings. It never writes report prose, Markdown, DOCX, Evidence, or Claims.
        """

        try:
            outline = _build_current_report_outline(composition, runtime.state, runtime.context)
            contract = composition.save_report_outline(runtime.state, runtime.context, outline)
        except (RuntimeError, TypeError, ValueError, WorkCompositionError) as exc:
            return str(exc)
        return _format_outline(contract.contract_version, contract.status.value, outline)

    @tool("get_current_report_outline")
    def get_current_report_outline(runtime: ToolRuntime) -> str:
        """Read the current ReportOutline ledger contract without generating report prose."""

        item = composition.current_work(runtime.state, runtime.context)
        if item is None:
            return "当前员工没有可见的进行中报告任务。"
        contract = composition.repository.get_current_work_contract(
            tenant_id=item.tenant_id,
            work_id=item.work_id,
            contract_type=REPORT_OUTLINE_CONTRACT_TYPE,
        )
        if contract is None:
            return "当前任务尚未形成 ReportOutline。"
        try:
            outline = ReportOutline.from_contract(contract)
        except (TypeError, ValueError) as exc:
            return f"当前 ReportOutline 合同无效：{exc}"
        return _format_outline(contract.contract_version, contract.status.value, outline)

    @tool("confirm_report_outline")
    def confirm_report_outline(expected_version: int, runtime: ToolRuntime) -> str:
        """Confirm the exact current ReportOutline after explicit requester approval.

        When the latest employee message names a ReportOutline version with confirmation
        intent, call this tool with that version. Do not validate case, spacing, or
        spelling yourself; the server parser is the sole authority and fails closed.
        The server also rechecks the current ReportBrief, gap decision, and source-set
        digest. Confirmation permits the later draft slice; this tool itself never
        creates report prose.
        """

        try:
            contract = composition.confirm_report_outline(
                runtime.state,
                runtime.context,
                expected_version=expected_version,
                latest_user_message=_latest_user_message_text(runtime),
            )
        except (TypeError, ValueError, WorkCompositionError) as exc:
            return str(exc)
        return (
            f"ReportOutline v{contract.contract_version} 已由任务委派人确认。"
            "本轮只完成提纲确认，不会自动生成初稿。"
            "如需继续，请在后续消息中显式请求“生成可审阅初稿”。"
        )

    @tool("prepare_report_draft_context")
    async def prepare_report_draft_context(runtime: ToolRuntime) -> str:
        """Prepare bounded, source-verified EvidenceRecords for the confirmed outline.

        Call only after the employee explicitly requests a review draft, and before
        build_report_draft. It re-reads only the department-knowledge
        chunks already selected by the confirmed ReportOutline, verifies their content
        hashes, and registers immutable EvidenceRecords. The returned excerpts are the
        only material allowed for factual or inferential draft claims. It never calls
        Gildata or Tavily and never writes report prose.
        """

        async def invoke(server: str, tool_name: str, arguments: dict, confirmed: bool) -> str:
            return await call_mcp_tool(server, tool_name, arguments, confirmed)

        try:
            context = await _prepare_report_draft_context(
                composition=composition,
                state=runtime.state,
                runtime_context=runtime.context,
                latest_user_message=_latest_user_message_text(runtime),
                invoke_mcp=invoke,
            )
        except (RuntimeError, TypeError, ValueError, WorkCompositionError) as exc:
            return str(exc)
        return json.dumps(context.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)

    @tool("build_report_draft")
    def build_report_draft(claims: list[DraftClaimProposal], runtime: ToolRuntime) -> str:
        """Validate structured Claims and save one ledger-backed Markdown ReportDraft.

        Requires a confirmed current ReportOutline and EvidenceRecords prepared by
        prepare_report_draft_context. Every fact or inference must bind evidence_ids
        returned by that tool and use the exact confirmed section_id. Recommendations
        and risks may omit evidence but remain unverified. The server renders citations,
        unresolved questions, and deterministic quality checks. Relay the returned
        ReportDraft block verbatim; never embellish or rewrite it. This tool does
        not generate DOCX/PDF, submit or approve the draft, or deliver an artifact.
        """

        try:
            draft = _build_report_draft(
                composition=composition,
                state=runtime.state,
                runtime_context=runtime.context,
                latest_user_message=_latest_user_message_text(runtime),
                proposals=claims,
            )
            contract = composition.save_report_draft(runtime.state, runtime.context, draft)
        except (RuntimeError, TypeError, ValueError, WorkCompositionError) as exc:
            return str(exc)
        return _format_draft(contract.contract_version, contract.status.value, draft)

    @tool("confirm_report_draft")
    def confirm_report_draft(expected_version: int, runtime: ToolRuntime) -> str:
        """Confirm the exact current ReportDraft after explicit requester review.

        Call only when the latest employee message explicitly confirms
        `ReportDraft vN`; the server parser is the sole authority and fails closed.
        This records requester acceptance of the review draft. It is not final
        approval, publication, DOCX/PDF generation, or artifact delivery.
        """

        try:
            contract = composition.confirm_report_draft(
                runtime.state,
                runtime.context,
                expected_version=expected_version,
                latest_user_message=_latest_user_message_text(runtime),
            )
        except (TypeError, ValueError, WorkCompositionError) as exc:
            return str(exc)
        return (
            f"ReportDraft v{contract.contract_version} 已由任务委派人确认。"
            "这只表示当前 Markdown 初稿版本已确认，不是最终批准，"
            "也不会生成或交付 DOCX/PDF。"
            f"如需进入内容审批，请在后续消息中显式回复“提交 ReportDraft v{contract.contract_version} 审批”。"
        )

    @tool("get_current_report_draft")
    def get_current_report_draft(runtime: ToolRuntime) -> str:
        """Read the current ledger-backed ReportDraft and return its exact Markdown."""

        item = composition.current_work(runtime.state, runtime.context)
        if item is None:
            return "当前员工没有可见的进行中报告任务。"
        contract = composition.repository.get_current_work_contract(
            tenant_id=item.tenant_id,
            work_id=item.work_id,
            contract_type=REPORT_DRAFT_CONTRACT_TYPE,
        )
        if contract is None:
            return "当前任务尚未形成 ReportDraft。"
        try:
            draft = ReportDraft.from_contract(contract)
        except (TypeError, ValueError) as exc:
            return f"当前 ReportDraft 合同无效：{exc}"
        return _format_draft(contract.contract_version, contract.status.value, draft)

    @tool("request_report_approval")
    def request_report_approval(expected_version: int, runtime: ToolRuntime) -> str:
        """Submit one exact confirmed ReportDraft version for content approval.

        Call only when the latest employee message explicitly says
        `提交 ReportDraft vN 审批` (or an equivalent exact-version approval request).
        This creates a pending approval contract. It does not approve, render,
        publish, or deliver anything.
        """

        try:
            contract = composition.request_report_approval(
                runtime.state,
                runtime.context,
                expected_version=expected_version,
                latest_user_message=_latest_user_message_text(runtime),
            )
        except (TypeError, ValueError, WorkCompositionError) as exc:
            return str(exc)
        return (
            f"ReportApproval contract_v{contract.contract_version}，"
            f"status={approval_state(contract)}，bound_report_draft_v{expected_version}。"
            "该合同只申请内容审批，不代表已批准，也未生成、发布或交付文件。"
            f"审批人如批准，请另行明确回复“批准 ReportDraft v{expected_version}”。"
        )

    @tool("get_current_report_approval")
    def get_current_report_approval(runtime: ToolRuntime) -> str:
        """Read the current versioned report-content approval from the ledger."""

        summary = composition.current_work_summary(runtime.state, runtime.context)
        if summary is None:
            return "当前员工没有可见的进行中报告任务。"
        approval = summary.get("report_approval")
        if not isinstance(approval, Mapping):
            return "当前任务尚未形成 ReportApproval。"
        return (
            f"ReportApproval contract_v{approval.get('contract_version')}，"
            f"status={approval.get('status')}，"
            f"bound_report_draft_v{approval.get('report_draft_version')}，"
            f"current={str(bool(approval.get('current'))).lower()}，"
            f"policy={approval.get('policy_id')}。"
        )

    @tool("approve_report_draft")
    def approve_report_draft(expected_version: int, runtime: ToolRuntime) -> str:
        """Approve one exact pending ReportDraft as the WorkItem approver.

        Call only when the latest employee message explicitly says
        `批准 ReportDraft vN`. The server validates the authenticated approver,
        current draft digest, pending approval contract, and exact version. Approval
        covers report content only; it never renders, publishes, or delivers files.
        """

        try:
            contract = composition.approve_report_draft(
                runtime.state,
                runtime.context,
                expected_version=expected_version,
                latest_user_message=_latest_user_message_text(runtime),
            )
        except (TypeError, ValueError, WorkCompositionError) as exc:
            return str(exc)
        return (
            f"ReportApproval contract_v{contract.contract_version}，status=approved，"
            f"bound_report_draft_v{expected_version}。ReportDraft v{expected_version} 内容已批准。"
            "批准不等于发布或交付，本轮不会生成 DOCX/PDF，也不会发送文件。"
            f"如需生成文件，请另行回复“生成 ReportDraft v{expected_version} DOCX”。"
        )

    @tool("render_report_docx_artifact")
    def render_report_docx_artifact(expected_version: int, runtime: ToolRuntime) -> str:
        """Render one exact approved, current ReportDraft into an immutable DOCX.

        Call only when the latest employee message explicitly asks to generate or
        export `ReportDraft vN` as DOCX/Word. The server requires a current approved
        ReportApproval and exact version/digest bindings. Rendering creates an
        Artifact ledger record only; it never publishes or delivers the file.
        """

        try:
            artifact = composition.render_report_artifact(
                runtime.state,
                runtime.context,
                expected_version=expected_version,
                artifact_format="docx",
                latest_user_message=_latest_user_message_text(runtime),
            )
        except (OSError, TypeError, ValueError, WorkCompositionError) as exc:
            return str(exc)
        return (
            f"ReportArtifact artifact_id={artifact.artifact_id}，format=docx，"
            f"bound_report_draft_v{artifact.source_contract_version}，"
            f"content_sha256={artifact.content_sha256}，size_bytes={artifact.size_bytes}，"
            "current=true，publication=not_published，delivery=not_delivered。"
            "DOCX Artifact 已生成并登记，但尚未发布或交付。"
        )

    @tool("get_current_report_artifacts")
    def get_current_report_artifacts(runtime: ToolRuntime) -> str:
        """Read immutable report Artifact metadata without exposing host paths."""

        summary = composition.current_work_summary(runtime.state, runtime.context)
        if summary is None:
            return "当前员工没有可见的进行中报告任务。"
        artifacts = summary.get("report_artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            return "当前任务尚未生成 ReportArtifact。"
        lines: list[str] = []
        for artifact in artifacts:
            if not isinstance(artifact, Mapping):
                continue
            lines.append(
                "ReportArtifact "
                f"artifact_id={artifact.get('artifact_id')}，format={artifact.get('format')}，"
                f"bound_report_draft_v{artifact.get('report_draft_version')}，"
                f"content_sha256={artifact.get('content_sha256')}，"
                f"size_bytes={artifact.get('size_bytes')}，"
                f"current={str(bool(artifact.get('current'))).lower()}，"
                f"publication={artifact.get('publication_status')}，"
                f"delivery={artifact.get('delivery_status')}。"
            )
        return "\n".join(lines)

    @tool("publish_report_artifact")
    def publish_report_artifact(expected_version: int, runtime: ToolRuntime) -> str:
        """Publish one exact current ReportArtifact without delivering it.

        Call only when the latest employee message is exactly
        `发布 ReportArtifact vN`. The server revalidates the current approved
        Draft, Artifact bytes and all immutable digest bindings. Publication is
        a ledger fact only; this tool never sends a card or creates a download URL.
        """

        try:
            publication = composition.publish_report_artifact(
                runtime.state,
                runtime.context,
                expected_version=expected_version,
                latest_user_message=_latest_user_message_text(runtime),
            )
        except (OSError, TypeError, ValueError, WorkCompositionError) as exc:
            return str(exc)
        return (
            f"ReportPublication publication_id={publication.publication_id}，"
            f"publication_v{publication.publication_version}，status=published，"
            f"artifact_id={publication.artifact_id}，"
            f"bound_report_draft_v{publication.source_contract_version}，"
            f"content_sha256={publication.content_sha256}，current=true，"
            "delivery=not_delivered。ReportArtifact 已正式发布，但尚未交付；"
            "本轮不会发送模板卡片、文件或下载链接。"
        )

    @tool("get_current_report_publications")
    def get_current_report_publications(runtime: ToolRuntime) -> str:
        """Read publication ledger state without publishing or delivering."""

        summary = composition.current_work_summary(runtime.state, runtime.context)
        if summary is None:
            return "当前员工没有可见的进行中报告任务。"
        publications = summary.get("report_publications")
        if not isinstance(publications, list) or not publications:
            return "当前任务尚无 ReportPublication；Artifact 尚未发布。"
        lines: list[str] = []
        for publication in publications:
            if not isinstance(publication, Mapping):
                continue
            lines.append(
                "ReportPublication "
                f"publication_id={publication.get('publication_id')}，"
                f"publication_v{publication.get('publication_version')}，"
                f"status={publication.get('status')}，"
                f"artifact_id={publication.get('artifact_id')}，"
                f"bound_report_draft_v{publication.get('report_draft_version')}，"
                f"content_sha256={publication.get('content_sha256')}，"
                f"current={str(bool(publication.get('current'))).lower()}，"
                f"delivery={publication.get('delivery_status')}。"
            )
        return "\n".join(lines)

    return [
        create_industry_report_work,
        get_current_work_status,
        save_report_brief,
        confirm_report_brief,
        run_internal_report_research,
        get_report_research_gaps,
        resolve_report_research_gaps,
        build_report_outline,
        get_current_report_outline,
        confirm_report_outline,
        prepare_report_draft_context,
        build_report_draft,
        get_current_report_draft,
        confirm_report_draft,
        request_report_approval,
        get_current_report_approval,
        approve_report_draft,
        render_report_docx_artifact,
        get_current_report_artifacts,
        publish_report_artifact,
        get_current_report_publications,
    ]


def _build_current_report_outline(
    composition: IndustryReportWorkComposition,
    state: Mapping[str, object],
    runtime_context: object | None,
) -> ReportOutline:
    internal = _load_current_research_result(
        composition=composition,
        state=state,
        runtime_context=runtime_context,
        template_path=composition.research_template_path,
    )
    item = composition.current_work(state, runtime_context)
    if item is None:
        raise WorkCompositionError("当前员工没有可形成提纲的进行中报告任务。")
    gaps = internal.coverage.gaps
    gap_contract_version = _require_outline_gap_decision(composition, item, internal) if gaps else None
    sources = _current_outline_sources(
        composition=composition,
        tenant_id=item.tenant_id,
        work_id=item.work_id,
        report_brief_version=internal.plan.report_brief_version,
        research_plan_digest=internal.plan.digest,
        gap_decision_contract_version=gap_contract_version,
    )
    source_ids_by_question: dict[str, list[str]] = {}
    for source in sources:
        question_ids = source.metadata.get("question_ids")
        if not isinstance(question_ids, list):
            continue
        for question_id in question_ids:
            source_ids_by_question.setdefault(str(question_id), []).append(source.source_id)
    sections: list[OutlineSection] = []
    for section in internal.plan.template.sections:
        questions = tuple(
            OutlineQuestion(
                question_id=question.question_id,
                prompt=question.prompt,
                source_ids=tuple(dict.fromkeys(source_ids_by_question.get(question.question_id, []))),
            )
            for question in section.questions
            if internal.plan.research_scope in question.applies_to
        )
        if questions:
            sections.append(OutlineSection(section.section_id, section.title, questions))
    return ReportOutline(
        report_brief_version=internal.plan.report_brief_version,
        research_plan_digest=internal.plan.digest,
        research_scope=internal.plan.research_scope.value,
        report_title=internal.plan.report_title,
        template_id=internal.plan.template.template_id,
        template_version=internal.plan.template.template_version,
        source_set_digest=source_set_digest(sources),
        sections=tuple(sections),
        gap_decision_contract_version=gap_contract_version,
    )


def _require_outline_gap_decision(
    composition: IndustryReportWorkComposition,
    item: WorkItem,
    internal: InternalResearchResult,
) -> int:
    contract = composition.repository.get_current_work_contract(
        tenant_id=item.tenant_id,
        work_id=item.work_id,
        contract_type=RESEARCH_GAP_DECISION_CONTRACT_TYPE,
    )
    if contract is None or contract.status is not WorkContractStatus.CONFIRMED:
        raise WorkCompositionError("内部研究仍有缺口；请先对当前 ReportBrief 明确选择并确认缺口处理方式。")
    decision = ResearchGapDecision.from_contract(contract)
    plan = internal.plan
    gaps = internal.coverage.gaps
    if (
        decision.report_brief_version != plan.report_brief_version
        or decision.research_plan_digest != plan.digest
        or decision.gap_question_ids != gaps
        or decision.gap_digest != gap_digest(gaps)
    ):
        raise WorkCompositionError("当前研究缺口决策与最新 ReportBrief 或研究计划不一致，请重新选择。")
    if decision.action is ResearchGapAction.UPLOAD_MATERIALS:
        raise WorkCompositionError("当前缺口决策正在等待员工上传材料；材料入账并重新研究前不能形成提纲。")
    if decision.action in {ResearchGapAction.GILDATA, ResearchGapAction.PUBLIC_WEB}:
        sources = _current_outline_sources(
            composition=composition,
            tenant_id=item.tenant_id,
            work_id=item.work_id,
            report_brief_version=plan.report_brief_version,
            research_plan_digest=plan.digest,
            gap_decision_contract_version=contract.contract_version,
        )
        externally_supported: set[str] = set()
        for source in sources:
            if source.source_type is SourceType.DEPARTMENT_KNOWLEDGE:
                continue
            question_ids = source.metadata.get("question_ids")
            if isinstance(question_ids, list):
                externally_supported.update(str(question_id) for question_id in question_ids)
        missing = tuple(question_id for question_id in gaps if question_id not in externally_supported)
        if missing:
            raise WorkCompositionError(
                "外部检索决策尚未为全部缺口登记 SourceRecord，不能形成提纲："
                + "、".join(missing)
            )
    return contract.contract_version


def _current_outline_sources(
    *,
    composition: IndustryReportWorkComposition,
    tenant_id: str,
    work_id: str,
    report_brief_version: int,
    research_plan_digest: str,
    gap_decision_contract_version: int | None,
) -> tuple[SourceRecord, ...]:
    return tuple(
        source
        for source in composition.repository.list_source_records(tenant_id=tenant_id, work_id=work_id)
        if source.metadata.get("report_brief_version") == report_brief_version
        and source.metadata.get("research_plan_digest") == research_plan_digest
        and (
            source.source_type is SourceType.DEPARTMENT_KNOWLEDGE
            or (
                gap_decision_contract_version is not None
                and source.metadata.get("gap_decision_contract_version") == gap_decision_contract_version
            )
        )
    )


def _format_outline(contract_version: int, status: str, outline: ReportOutline) -> str:
    lines = [
        f"ReportOutline v{contract_version}，status={status}，"
        f"bound_report_brief_v{outline.report_brief_version}，"
        f"sources={len(outline.source_ids)}，unresolved={len(outline.unresolved_question_ids)}。"
    ]
    lines.extend(
        f"- {section.section_id}｜{section.title}｜{section.status.value}｜"
        f"sources={len(section.source_ids)}｜unresolved={len(section.unresolved_question_ids)}"
        for section in outline.sections
    )
    lines.append("该提纲仅含章节、研究问题和 SourceRecord 绑定，不含报告正文。")
    if status == WorkContractStatus.PROVISIONAL.value:
        lines.append(f"如认可，请明确回复“确认 ReportOutline v{contract_version}”。")
    return "\n".join(lines)


def _format_draft(contract_version: int, status: str, draft: ReportDraft) -> str:
    lines = [
        f"ReportDraft v{contract_version}，status={status}，"
        f"bound_report_outline_v{draft.report_outline_version}，"
        f"quality={draft.quality_status.value}，claims={len(draft.claim_ids)}。",
        "该版本是可审阅 Markdown 初稿，不是最终批准稿，也未生成 DOCX/PDF。",
        REPORT_DRAFT_MARKDOWN_BEGIN,
        draft.markdown,
        REPORT_DRAFT_MARKDOWN_END,
    ]
    if status == WorkContractStatus.PROVISIONAL.value:
        lines.append(
            f'如认可该初稿，请明确回复“确认 ReportDraft v{contract_version}”。'
        )
    elif status == WorkContractStatus.CONFIRMED.value:
        lines.append(
            f'如需进入内容审批，请另行回复“提交 ReportDraft v{contract_version} 审批”。'
        )
    return "\n".join(lines)


def _latest_user_message_text(runtime: ToolRuntime) -> str:
    state = runtime.state
    if not isinstance(state, Mapping):
        return ""
    messages = state.get("messages")
    if not isinstance(messages, (list, tuple)):
        return ""
    for message in reversed(messages):
        if isinstance(message, Mapping):
            role = str(message.get("role") or message.get("type") or "").lower()
            content = message.get("content", "")
        else:
            role = str(getattr(message, "type", "") or getattr(message, "role", "")).lower()
            content = getattr(message, "content", "")
        if role in {"human", "user"}:
            return _message_content_text(content)
    return ""


def _message_content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return " ".join(parts)
    return str(content or "")
