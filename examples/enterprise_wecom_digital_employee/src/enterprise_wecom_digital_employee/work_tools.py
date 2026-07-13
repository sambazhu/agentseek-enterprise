from __future__ import annotations

from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import ToolRuntime

from enterprise_wecom_digital_employee.work_composition import (
    IndustryReportWorkComposition,
    WorkCompositionError,
)


def work_tools(composition: IndustryReportWorkComposition) -> list[BaseTool]:
    """Return requester-scoped tools backed by the durable WorkItem ledger."""

    @tool("create_industry_report_work")
    def create_industry_report_work(runtime: ToolRuntime) -> str:
        """Create the tracked formal securities-industry report task requested in this turn.

        Use only when the employee explicitly asks to create, write, prepare, track,
        or audit a complete formal industry report. This server-side tool is fixed
        to work_mode=required. It cannot run as a DirectTurn and does not start
        research or invent a ReportBrief.
        """

        try:
            result = composition.create_report_work(runtime.state, runtime.context)
        except WorkCompositionError as exc:
            return str(exc)
        verb = "已创建" if result.created else "已找到同一幂等请求创建的"
        return (
            f"{verb}正式报告任务：work_id={result.item.work_id}，"
            f"status={result.item.status.value}，phase={result.item.current_phase}。"
            "任务已固定数字员工、PackSnapshot、Playbook、Skill digest、运行版本和角色映射。"
            "M1 阶段尚未实现 ReportBrief 与研究执行，请勿声称报告已经开始编写或已经完成。"
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

    return [create_industry_report_work, get_current_work_status]
