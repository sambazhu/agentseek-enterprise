from __future__ import annotations

from typing import Any

from enterprise_wecom_digital_employee.report_output_guard import (
    M2_OUTPUT_BLOCKED_MESSAGE,
    enforce_m2_output_guard,
)
from langchain_core.messages import AIMessage, HumanMessage


def _result(user_message: str, output: str, *, status: str = "draft", phase: str = "intake") -> dict[str, Any]:
    return {
        "current_work": {
            "work_id": "work_test",
            "status": status,
            "current_phase": phase,
        },
        "messages": [HumanMessage(content=user_message), AIMessage(content=output)],
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
    assert events == [(
        "report_output_guard",
        {
            "status": "blocked",
            "reason": "generic_confirmation",
            "work_id": "work_test",
            "phase": "intake",
            "output_chars": 8,
        },
    )]


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


def test_terminal_or_non_intake_work_is_not_guarded() -> None:
    output = "# 已批准交付物"

    assert enforce_m2_output_guard(
        _result("查看已交付报告", output, status="succeeded", phase="delivery"),
        output,
        event_sink=lambda *_args, **_kwargs: None,
    ) == output
