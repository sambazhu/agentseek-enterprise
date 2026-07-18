from __future__ import annotations

from typing import Any

from enterprise_wecom_digital_employee.report_output_guard import (
    M2_OUTPUT_BLOCKED_MESSAGE,
    REPORT_BRIEF_LEDGER_CLAIM_BLOCKED_MESSAGE,
    REPORT_OUTLINE_LEDGER_CLAIM_BLOCKED_MESSAGE,
    enforce_m2_output_guard,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


def _result(
    user_message: str,
    output: str,
    *,
    status: str = "draft",
    phase: str = "intake",
    tool_names: tuple[str, ...] = (),
) -> dict[str, Any]:
    messages: list[Any] = [HumanMessage(content=user_message)]
    if tool_names:
        messages.append(AIMessage(
            content="",
            tool_calls=[
                {"name": name, "args": {}, "id": f"call_{index}", "type": "tool_call"}
                for index, name in enumerate(tool_names)
            ],
        ))
    messages.append(AIMessage(content=output))
    return {
        "current_work": {
            "work_id": "work_test",
            "status": status,
            "current_phase": phase,
        },
        "messages": messages,
    }


def test_generic_confirmation_is_blocked_and_audited() -> None:
    events: list[tuple[str, dict[str, object]]] = []

    def sink(event: str, **fields: object) -> None:
        events.append((event, fields))

    guarded = enforce_m2_output_guard(
        _result("确认", "报告已编写完成。"),
        "报告已编写完成。",
        event_sink=sink,
    )

    assert guarded == M2_OUTPUT_BLOCKED_MESSAGE
    assert len(events) == 1
    event, fields = events[0]
    assert event == "report_output_guard"
    assert fields["status"] == "blocked"
    assert fields["reason"] == "generic_confirmation"
    assert fields["work_id"] == "work_test"
    assert fields["phase"] == "intake"
    assert fields["output_chars"] == 8
    assert fields["output_lines"] == 1
    assert str(fields["output_digest"]).startswith("sha256:")
    assert fields["diagnostic_signals"] == ["report_phrase:报告已编写完成"]
    assert fields["tool_sequence"] == []


def test_generic_confirmation_uses_explicit_live_state_when_messages_drop_human_input() -> None:
    events: list[tuple[str, dict[str, object]]] = []
    result = _result("historical request", "我会继续处理当前任务。")
    result["messages"] = [AIMessage(content="我会继续处理当前任务。")]
    result["latest_user_message"] = "确认"

    guarded = enforce_m2_output_guard(
        result,
        "我会继续处理当前任务。",
        event_sink=lambda event, **fields: events.append((event, fields)),
    )

    assert guarded == M2_OUTPUT_BLOCKED_MESSAGE
    assert events[0][1]["reason"] == "generic_confirmation"


def test_markdown_report_body_and_unsupported_figures_are_blocked() -> None:
    output = """# 完整报告正文

## 执行摘要
美联储利率为 4.50%-4.75%。

## 行动建议
预计营收增长 10%-15%。
"""

    assert enforce_m2_output_guard(
        _result("请生成报告", output),
        output,
        event_sink=lambda *_args, **_kwargs: None,
    ) == M2_OUTPUT_BLOCKED_MESSAGE


def test_versioned_confirmation_and_operational_reply_are_allowed() -> None:
    output = "ReportBrief v1 已确认，内部知识检索覆盖 5/6，尚有 1 个主题证据缺口。"

    assert enforce_m2_output_guard(
        _result("确认 ReportBrief v1", output),
        output,
        event_sink=lambda *_args, **_kwargs: None,
    ) == output


def test_unverified_report_brief_save_claim_is_blocked_and_audited() -> None:
    events: list[tuple[str, dict[str, object]]] = []
    output = "ReportBrief v6 已保存，输出格式已更新为 Markdown 和 DOCX。"

    guarded = enforce_m2_output_guard(
        _result("改为 Markdown 和 DOCX", output),
        output,
        event_sink=lambda event, **fields: events.append((event, fields)),
    )

    assert guarded == REPORT_BRIEF_LEDGER_CLAIM_BLOCKED_MESSAGE
    assert events[0][1]["reason"] == "unverified_report_brief_write"
    assert events[0][1]["tool_sequence"] == []
    assert output not in str(events[0][1])


def test_report_brief_save_claim_requires_matching_successful_tool_result() -> None:
    output = "ReportBrief v6 已保存，输出格式已更新为 Markdown 和 DOCX。"
    result = _result("改为 Markdown 和 DOCX", output)
    result["messages"] = [
        HumanMessage(content="改为 Markdown 和 DOCX"),
        AIMessage(content="", tool_calls=[{
            "name": "save_report_brief",
            "args": {},
            "id": "call_save",
            "type": "tool_call",
        }]),
        ToolMessage(
            content="ReportBrief v6 已保存，状态=provisional。",
            name="save_report_brief",
            tool_call_id="call_save",
        ),
        AIMessage(content=output),
    ]

    assert enforce_m2_output_guard(
        result,
        output,
        event_sink=lambda *_args, **_kwargs: None,
    ) == output


def test_report_brief_save_claim_rejects_mismatched_tool_result_version() -> None:
    output = "ReportBrief v7 已保存，输出格式已更新为 XLSX。"
    result = _result("输出为 XLSX", output)
    result["messages"] = [
        HumanMessage(content="输出为 XLSX"),
        AIMessage(content="", tool_calls=[{
            "name": "save_report_brief",
            "args": {},
            "id": "call_save",
            "type": "tool_call",
        }]),
        ToolMessage(
            content="output_formats contains an unsupported format",
            name="save_report_brief",
            tool_call_id="call_save",
        ),
        AIMessage(content=output),
    ]

    assert enforce_m2_output_guard(
        result,
        output,
        event_sink=lambda *_args, **_kwargs: None,
    ) == REPORT_BRIEF_LEDGER_CLAIM_BLOCKED_MESSAGE


def test_unverified_report_outline_generation_claim_is_blocked_and_audited() -> None:
    events: list[tuple[str, dict[str, object]]] = []
    output = "ReportOutline v2 已生成并保存，状态=provisional。"

    guarded = enforce_m2_output_guard(
        _result("生成报告提纲", output),
        output,
        event_sink=lambda event, **fields: events.append((event, fields)),
    )

    assert guarded == REPORT_OUTLINE_LEDGER_CLAIM_BLOCKED_MESSAGE
    assert events[0][1]["reason"] == "unverified_report_outline_write"
    assert output not in str(events[0][1])


def test_report_outline_generation_claim_requires_matching_build_result() -> None:
    output = "ReportOutline v2 已生成并保存，状态=provisional。"
    result = _result("生成报告提纲", output)
    result["messages"] = [
        HumanMessage(content="生成报告提纲"),
        AIMessage(content="", tool_calls=[{
            "name": "build_report_outline",
            "args": {},
            "id": "call_build",
            "type": "tool_call",
        }]),
        ToolMessage(
            content="ReportOutline v2，status=provisional，绑定 ReportBrief v6。",
            name="build_report_outline",
            tool_call_id="call_build",
        ),
        AIMessage(content=output),
    ]

    assert enforce_m2_output_guard(
        result,
        output,
        event_sink=lambda *_args, **_kwargs: None,
    ) == output


def test_report_outline_confirmation_claim_rejects_only_provisional_build_result() -> None:
    output = "ReportOutline v2 已由任务委派人确认。"
    result = _result("确认 ReportOutline v2", output)
    result["messages"] = [
        HumanMessage(content="确认 ReportOutline v2"),
        AIMessage(content="", tool_calls=[{
            "name": "build_report_outline",
            "args": {},
            "id": "call_build",
            "type": "tool_call",
        }]),
        ToolMessage(
            content="ReportOutline v2，status=provisional，绑定 ReportBrief v6。",
            name="build_report_outline",
            tool_call_id="call_build",
        ),
        AIMessage(content=output),
    ]

    assert enforce_m2_output_guard(
        result,
        output,
        event_sink=lambda *_args, **_kwargs: None,
    ) == REPORT_OUTLINE_LEDGER_CLAIM_BLOCKED_MESSAGE


def test_report_outline_confirmation_claim_allows_matching_confirm_result() -> None:
    output = "ReportOutline v2 已由任务委派人确认。"
    result = _result("确认 ReportOutline v2", output)
    result["messages"] = [
        HumanMessage(content="确认 ReportOutline v2"),
        AIMessage(content="", tool_calls=[{
            "name": "confirm_report_outline",
            "args": {"expected_version": 2},
            "id": "call_confirm",
            "type": "tool_call",
        }]),
        ToolMessage(
            content="ReportOutline v2 已由任务委派人确认。",
            name="confirm_report_outline",
            tool_call_id="call_confirm",
        ),
        AIMessage(content=output),
    ]

    assert enforce_m2_output_guard(
        result,
        output,
        event_sink=lambda *_args, **_kwargs: None,
    ) == output


def test_report_outline_read_claim_allows_matching_current_ledger_result() -> None:
    output = "当前 ReportOutline v2 状态=confirmed，绑定 ReportBrief v6。"
    result = _result("查看当前报告提纲", output)
    result["messages"] = [
        HumanMessage(content="查看当前报告提纲"),
        AIMessage(content="", tool_calls=[{
            "name": "get_current_report_outline",
            "args": {},
            "id": "call_get",
            "type": "tool_call",
        }]),
        ToolMessage(
            content="ReportOutline v2，status=confirmed，绑定 ReportBrief v6。",
            name="get_current_report_outline",
            tool_call_id="call_get",
        ),
        AIMessage(content=output),
    ]

    assert enforce_m2_output_guard(
        result,
        output,
        event_sink=lambda *_args, **_kwargs: None,
    ) == output


def test_report_outline_claim_allows_matching_work_status_result() -> None:
    output = "当前 ReportOutline v2 已确认，绑定 ReportBrief v6。"
    result = _result("查看当前工作状态", output)
    result["messages"] = [
        HumanMessage(content="查看当前工作状态"),
        AIMessage(content="", tool_calls=[{
            "name": "get_current_work_status",
            "args": {},
            "id": "call_status",
            "type": "tool_call",
        }]),
        ToolMessage(
            content=(
                "当前 WorkItem：work_test，status=draft，phase=intake，version=0。\n"
                "当前 ReportOutline：v2，status=confirmed，"
                "bound_report_brief_v6，unresolved=1。"
            ),
            name="get_current_work_status",
            tool_call_id="call_status",
        ),
        AIMessage(content=output),
    ]

    assert enforce_m2_output_guard(
        result,
        output,
        event_sink=lambda *_args, **_kwargs: None,
    ) == output


def test_work_status_provisional_outline_cannot_support_confirmed_claim() -> None:
    output = "当前 ReportOutline v2 已确认。"
    result = _result("查看当前工作状态", output)
    result["messages"] = [
        HumanMessage(content="查看当前工作状态"),
        AIMessage(content="", tool_calls=[{
            "name": "get_current_work_status",
            "args": {},
            "id": "call_status",
            "type": "tool_call",
        }]),
        ToolMessage(
            content="当前 ReportOutline：v2，status=provisional，bound_report_brief_v6。",
            name="get_current_work_status",
            tool_call_id="call_status",
        ),
        AIMessage(content=output),
    ]

    assert enforce_m2_output_guard(
        result,
        output,
        event_sink=lambda *_args, **_kwargs: None,
    ) == REPORT_OUTLINE_LEDGER_CLAIM_BLOCKED_MESSAGE


def test_work_status_outline_claim_rejects_mismatched_version() -> None:
    output = "当前 ReportOutline v3 已确认。"
    result = _result("查看当前工作状态", output)
    result["messages"] = [
        HumanMessage(content="查看当前工作状态"),
        AIMessage(content="", tool_calls=[{
            "name": "get_current_work_status",
            "args": {},
            "id": "call_status",
            "type": "tool_call",
        }]),
        ToolMessage(
            content="当前 ReportOutline：v2，status=confirmed，bound_report_brief_v6。",
            name="get_current_work_status",
            tool_call_id="call_status",
        ),
        AIMessage(content=output),
    ]

    assert enforce_m2_output_guard(
        result,
        output,
        event_sink=lambda *_args, **_kwargs: None,
    ) == REPORT_OUTLINE_LEDGER_CLAIM_BLOCKED_MESSAGE


def test_coverage_table_and_section_labels_are_allowed_with_safe_diagnostics() -> None:
    events: list[tuple[str, dict[str, object]]] = []

    def sink(event: str, **fields: object) -> None:
        events.append((event, fields))

    output = """ReportBrief v1 内部研究覆盖 5/6，存在 1 个主题证据缺口。

| 章节 | 状态 |
| --- | --- |
| 执行摘要 | 缺口 |
| 行业发展概况 | 已覆盖 |
| 经营差异 | 已覆盖 |

可选择 Gildata、Tavily、上传材料或保留缺口。"""
    result = _result(
        "确认 ReportBrief v1",
        output,
        tool_names=(
            "confirm_report_brief",
            "run_internal_report_research",
            "get_report_research_gaps",
        ),
    )
    result["messages"].insert(0, AIMessage(
        content="",
        tool_calls=[{
            "name": "historical_tool_call",
            "args": {},
            "id": "call_history",
            "type": "tool_call",
        }],
    ))
    guarded = enforce_m2_output_guard(
        result,
        output,
        event_sink=sink,
    )

    assert guarded == output
    assert len(events) == 1
    _, fields = events[0]
    assert fields["status"] == "allowed"
    assert fields["reason"] == "operational_response"
    assert fields["diagnostic_signals"] == ["markdown_table", "section_labels:3"]
    assert fields["tool_sequence"] == [
        "confirm_report_brief",
        "run_internal_report_research",
        "get_report_research_gaps",
    ]
    assert output not in str(fields)


def test_long_report_without_markdown_is_blocked() -> None:
    output = "研究结论。" * 250

    assert enforce_m2_output_guard(
        _result("请生成报告", output),
        output,
        event_sink=lambda *_args, **_kwargs: None,
    ) == M2_OUTPUT_BLOCKED_MESSAGE


def test_observability_failure_does_not_break_allowed_output() -> None:
    output = "内部研究覆盖 5/6，请选择一个版本绑定的缺口处理选项。"

    def broken_sink(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("collector unavailable")

    assert enforce_m2_output_guard(
        _result("确认 ReportBrief v1", output),
        output,
        event_sink=broken_sink,
    ) == output


def test_terminal_or_non_intake_work_is_not_guarded() -> None:
    output = "# 已批准交付物"

    assert enforce_m2_output_guard(
        _result("查看已交付报告", output, status="succeeded", phase="delivery"),
        output,
        event_sink=lambda *_args, **_kwargs: None,
    ) == output
