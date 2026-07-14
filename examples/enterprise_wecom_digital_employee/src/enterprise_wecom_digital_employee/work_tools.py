from __future__ import annotations

import json

from agentseek_work import ActiveWorkConflictError
from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import ToolRuntime

from enterprise_wecom_digital_employee.report_brief import CoveragePeriodSource, ReportBrief
from enterprise_wecom_digital_employee.report_research import run_internal_research as _run_internal_research
from enterprise_wecom_digital_employee.settings import PROJECT_ROOT
from enterprise_wecom_digital_employee.tools import call_mcp_tool
from enterprise_wecom_digital_employee.work_composition import (
    IndustryReportWorkComposition,
    WorkCompositionError,
)

_RESEARCH_TEMPLATE = (
    PROJECT_ROOT
    / "digital_employees"
    / "industry-report"
    / "skills"
    / "report-intake"
    / "references"
    / "internal-research-template.yaml"
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
    def get_current_work_status(runtime: ToolRuntime) -> str:
        """Read the authenticated employee's current non-terminal WorkItem status."""

        item = composition.current_work(runtime.state, runtime.context)
        if item is None:
            return "当前员工没有可见的进行中任务。"
        return (
            f"当前任务：work_id={item.work_id}，status={item.status.value}，"
            f"phase={item.current_phase}，playbook={item.playbook_id}@{item.playbook_version}。"
        )

    @tool("save_report_brief")
    def save_report_brief(
        title: str,
        target_audience: list[str],
        runtime: ToolRuntime,
        coverage_period: str = "",
        output_formats: list[str] | None = None,
        confidentiality_level: str = "internal",
    ) -> str:
        """Save or revise the lightweight ReportBrief for the current report WorkItem.

        Use the employee's stated topic and audience. coverage_period may be empty,
        in which case the approved playbook default is used. Saving is provisional:
        it does not authorize research. Show the returned version and summary to the
        employee and ask them to confirm that exact version.
        """

        clean_period = coverage_period.strip()
        brief = ReportBrief(
            title=title.strip(),
            target_audience=tuple(value.strip() for value in target_audience if value.strip()),
            coverage_period=clean_period or "截至请求时间的最新可得数据",
            coverage_period_source=(
                CoveragePeriodSource.EXPLICIT if clean_period else CoveragePeriodSource.PLAYBOOK_DEFAULT
            ),
            output_formats=tuple(output_formats or ["docx"]),
            confidentiality_level=confidentiality_level,
        )
        try:
            contract = composition.save_report_brief(runtime.state, runtime.context, brief)
        except (ValueError, WorkCompositionError) as exc:
            return str(exc)
        return (
            f"ReportBrief v{contract.contract_version} 已保存，状态={contract.status.value}。"
            f"主题：{brief.title}；目标受众：{'、'.join(brief.target_audience)}；"
            f"报告覆盖期：{brief.coverage_period}；输出：{','.join(brief.output_formats)}。"
            "请员工确认上述版本；未确认前不得启动正式知识检索。"
        )

    @tool("confirm_report_brief")
    def confirm_report_brief(expected_version: int, runtime: ToolRuntime) -> str:
        """Confirm the exact current ReportBrief version after explicit requester approval.

        Call only when the employee's latest message clearly confirms the exact
        ReportBrief summary and version. Never infer confirmation from earlier turns.
        """

        try:
            contract = composition.confirm_report_brief(
                runtime.state,
                runtime.context,
                expected_version=expected_version,
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
                template_path=_RESEARCH_TEMPLATE,
                invoke_mcp=invoke,
            )
        except (RuntimeError, ValueError, WorkCompositionError) as exc:
            return str(exc)
        return json.dumps(result.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)

    return [
        create_industry_report_work,
        get_current_work_status,
        save_report_brief,
        confirm_report_brief,
        run_internal_report_research,
    ]
