from __future__ import annotations

from typing import Any

from enterprise_wecom_digital_employee.report_output_guard import (
    M2_OUTPUT_BLOCKED_MESSAGE,
    enforce_m2_output_guard,
)
from langchain_core.messages import AIMessage, HumanMessage


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
