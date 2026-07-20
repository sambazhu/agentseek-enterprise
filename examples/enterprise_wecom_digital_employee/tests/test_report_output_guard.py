from __future__ import annotations

from typing import Any

from enterprise_wecom_digital_employee.report_output_guard import (
    M2_OUTPUT_BLOCKED_MESSAGE,
    REPORT_APPROVAL_LEDGER_CLAIM_BLOCKED_MESSAGE,
    REPORT_ARTIFACT_LEDGER_CLAIM_BLOCKED_MESSAGE,
    REPORT_BRIEF_LEDGER_CLAIM_BLOCKED_MESSAGE,
    REPORT_DRAFT_LEDGER_CLAIM_BLOCKED_MESSAGE,
    REPORT_OUTLINE_LEDGER_CLAIM_BLOCKED_MESSAGE,
    REPORT_PUBLICATION_LEDGER_CLAIM_BLOCKED_MESSAGE,
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


def test_ledger_backed_report_draft_replaces_model_rewrite_with_exact_tool_output() -> None:
    tool_output = """ReportDraft v1，status=provisional，bound_report_outline_v1，quality=warning，claims=1。
该版本是可审阅 Markdown 初稿，不是最终批准稿，也未生成 DOCX/PDF。
[REPORT_DRAFT_MARKDOWN]
# 证券行业报告

## 执行摘要

证据支持的初稿陈述。[E1]
[/REPORT_DRAFT_MARKDOWN]"""
    model_output = "# 完整报告正文\n\n模型擅自改写的内容。"
    result = _result("生成初稿", model_output)
    result["messages"] = [
        HumanMessage(content="生成初稿"),
        AIMessage(content="", tool_calls=[{
            "name": "build_report_draft",
            "args": {},
            "id": "call_draft",
            "type": "tool_call",
        }]),
        ToolMessage(
            content=tool_output,
            name="build_report_draft",
            tool_call_id="call_draft",
        ),
        AIMessage(content=model_output),
    ]

    assert enforce_m2_output_guard(
        result,
        model_output,
        event_sink=lambda *_args, **_kwargs: None,
    ) == tool_output


def test_report_draft_claim_without_ledger_result_is_blocked() -> None:
    output = "ReportDraft v9 已生成并保存，稍后可以下载。"

    assert enforce_m2_output_guard(
        _result("报告进度", output),
        output,
        event_sink=lambda *_args, **_kwargs: None,
    ) == REPORT_DRAFT_LEDGER_CLAIM_BLOCKED_MESSAGE


def test_report_draft_confirmation_claim_allows_matching_confirm_result() -> None:
    output = "ReportDraft v1 已由任务委派人确认。"
    result = _result("确认 ReportDraft v1", output)
    result["messages"] = [
        HumanMessage(content="确认 ReportDraft v1"),
        AIMessage(content="", tool_calls=[{
            "name": "confirm_report_draft",
            "args": {"expected_version": 1},
            "id": "call_confirm_draft",
            "type": "tool_call",
        }]),
        ToolMessage(
            content="ReportDraft v1 已由任务委派人确认。",
            name="confirm_report_draft",
            tool_call_id="call_confirm_draft",
        ),
        AIMessage(content=output),
    ]

    assert enforce_m2_output_guard(
        result,
        output,
        event_sink=lambda *_args, **_kwargs: None,
    ) == output


def test_outline_confirmation_appends_deterministic_next_step_nudge() -> None:
    output = "ReportOutline v1 已由任务委派人确认。"
    result = _result("确认 ReportOutline v1", output)
    result["messages"] = [
        HumanMessage(content="确认 ReportOutline v1"),
        AIMessage(content="", tool_calls=[{
            "name": "confirm_report_outline",
            "args": {"expected_version": 1},
            "id": "call_confirm_outline",
            "type": "tool_call",
        }]),
        ToolMessage(
            content="ReportOutline v1 已由任务委派人确认。本轮只完成提纲确认。",
            name="confirm_report_outline",
            tool_call_id="call_confirm_outline",
        ),
        AIMessage(content=output),
    ]

    guarded = enforce_m2_output_guard(
        result,
        output,
        event_sink=lambda *_args, **_kwargs: None,
    )

    assert guarded == (
        "ReportOutline v1 已由任务委派人确认。\n\n"
        "提纲已确认；如需初稿，请另行回复“生成可审阅初稿”。"
    )


def test_report_approval_claim_requires_exact_current_ledger_state() -> None:
    output = "ReportDraft v1 已批准。"
    result = _result("查看审批状态", output)
    result["current_work"].update({
        "report_draft": {"contract_version": 1, "status": "confirmed"},
        "report_approval": {
            "contract_version": 1,
            "status": "pending",
            "report_draft_version": 1,
            "current": True,
        },
    })

    assert enforce_m2_output_guard(
        result,
        output,
        event_sink=lambda *_args, **_kwargs: None,
    ) == REPORT_APPROVAL_LEDGER_CLAIM_BLOCKED_MESSAGE

    pending_output = "ReportDraft v1 已提交审批，当前待审批。"
    result["messages"][-1] = AIMessage(content=pending_output)
    assert enforce_m2_output_guard(
        result,
        pending_output,
        event_sink=lambda *_args, **_kwargs: None,
    ) == pending_output


def test_report_approval_claim_allows_matching_approve_tool_result() -> None:
    output = "ReportDraft v1 内容已批准，但尚未发布或交付。"
    result = _result("批准 ReportDraft v1", output)
    result["messages"] = [
        HumanMessage(content="批准 ReportDraft v1"),
        AIMessage(content="", tool_calls=[{
            "name": "approve_report_draft",
            "args": {"expected_version": 1},
            "id": "call_approve",
            "type": "tool_call",
        }]),
        ToolMessage(
            content=(
                "ReportApproval contract_v1，status=approved，bound_report_draft_v1。"
                "ReportDraft v1 内容已批准。"
            ),
            name="approve_report_draft",
            tool_call_id="call_approve",
        ),
        AIMessage(content=output),
    ]

    assert enforce_m2_output_guard(
        result,
        output,
        event_sink=lambda *_args, **_kwargs: None,
    ) == output


def test_stale_or_fake_report_approval_claim_is_blocked_and_digest_only() -> None:
    events: list[tuple[str, dict[str, object]]] = []
    output = "ReportDraft v999 已批准。"
    result = _result("查看审批状态", output)
    result["current_work"]["report_approval"] = {
        "contract_version": 1,
        "status": "approved",
        "report_draft_version": 1,
        "current": False,
    }

    guarded = enforce_m2_output_guard(
        result,
        output,
        event_sink=lambda event, **fields: events.append((event, fields)),
    )

    assert guarded == REPORT_APPROVAL_LEDGER_CLAIM_BLOCKED_MESSAGE
    assert events[0][1]["reason"] == "unverified_report_approval"
    assert output not in str(events[0][1])


def test_read_only_contract_status_prose_uses_server_published_ledger() -> None:
    output = (
        "ReportBrief v3 已保存，状态=confirmed。\n"
        "ReportOutline v2 已构建，暂定（provisional）。\n"
        "ReportDraft v1 已生成并保存，状态=provisional。"
    )
    result = _result("请简要说明当前合同状态", output)
    result["current_work"].update({
        "report_brief": {"contract_version": 3, "status": "confirmed"},
        "report_outline": {"contract_version": 2, "status": "provisional"},
        "report_draft": {"contract_version": 1, "status": "provisional"},
    })

    assert enforce_m2_output_guard(
        result,
        output,
        event_sink=lambda *_args, **_kwargs: None,
    ) == output


def test_server_published_ledger_does_not_allow_fake_or_wrong_draft_status() -> None:
    result = _result("查看初稿状态", "ReportDraft v999 已确认。")
    result["current_work"]["report_draft"] = {"contract_version": 1, "status": "provisional"}

    assert enforce_m2_output_guard(
        result,
        "ReportDraft v999 已确认。",
        event_sink=lambda *_args, **_kwargs: None,
    ) == REPORT_DRAFT_LEDGER_CLAIM_BLOCKED_MESSAGE

    result["messages"][-1] = AIMessage(content="ReportDraft v1 已确认。")
    assert enforce_m2_output_guard(
        result,
        "ReportDraft v1 已确认。",
        event_sink=lambda *_args, **_kwargs: None,
    ) == REPORT_DRAFT_LEDGER_CLAIM_BLOCKED_MESSAGE


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


def test_report_brief_save_claim_allows_matching_work_status_result() -> None:
    output = "当前 ReportBrief v2 已保存，状态=provisional。"
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
            content="当前 ReportBrief：v2，status=provisional。",
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


def test_work_status_report_brief_claim_rejects_mismatched_version() -> None:
    output = "当前 ReportBrief v3 已保存，状态=provisional。"
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
            content="当前 ReportBrief：v2，status=provisional。",
            name="get_current_work_status",
            tool_call_id="call_status",
        ),
        AIMessage(content=output),
    ]

    assert enforce_m2_output_guard(
        result,
        output,
        event_sink=lambda *_args, **_kwargs: None,
    ) == REPORT_BRIEF_LEDGER_CLAIM_BLOCKED_MESSAGE


def test_work_status_confirmed_brief_cannot_support_provisional_claim() -> None:
    output = "当前 ReportBrief v2 已保存，状态=provisional。"
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
            content="当前 ReportBrief：v2，status=confirmed。",
            name="get_current_work_status",
            tool_call_id="call_status",
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
    ) == (
        f"{output}\n\n"
        "提纲已确认；如需初稿，请另行回复“生成可审阅初稿”。"
    )


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


def test_work_status_provisional_outline_allows_matching_recorded_claim() -> None:
    output = "当前 ReportOutline v2 已生成并保存，状态=provisional。"
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
    ) == output


def test_adjacent_confirmed_brief_does_not_contaminate_provisional_outline() -> None:
    output = (
        "ReportBrief v1 已确认。\n"
        "ReportOutline v1 已构建，暂定（provisional）。"
    )
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
                "当前 ReportBrief：v1，status=confirmed。\n"
                "当前 ReportOutline：v1，status=provisional，"
                "bound_report_brief_v1。"
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


def test_adjacent_confirmed_outline_does_not_contaminate_provisional_brief() -> None:
    output = (
        "ReportOutline v1 已确认。\n"
        "ReportBrief v1 已暂存（provisional）。"
    )
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
                "当前 ReportOutline：v1，status=confirmed，bound_report_brief_v1。\n"
                "当前 ReportBrief：v1，status=provisional。"
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


def test_unverified_constructed_tentative_outline_is_blocked() -> None:
    output = "ReportOutline v999 已构建，暂定。"

    assert enforce_m2_output_guard(
        _result("查看当前工作状态", output),
        output,
        event_sink=lambda *_args, **_kwargs: None,
    ) == REPORT_OUTLINE_LEDGER_CLAIM_BLOCKED_MESSAGE


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


def test_work_status_confirmed_outline_cannot_support_provisional_claim() -> None:
    output = "当前 ReportOutline v2 已生成并保存，状态=provisional。"
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


def test_unverified_report_artifact_claim_is_blocked() -> None:
    output = "ReportDraft v999 DOCX Artifact 已生成。"

    assert enforce_m2_output_guard(
        _result("生成 ReportDraft v999 DOCX", output),
        output,
        event_sink=lambda *_args, **_kwargs: None,
    ) == REPORT_ARTIFACT_LEDGER_CLAIM_BLOCKED_MESSAGE


def test_ledger_backed_report_artifact_claim_is_allowed() -> None:
    artifact_id = f"artifact_{'a' * 64}"
    tool_output = (
        f"ReportArtifact artifact_id={artifact_id}，format=docx，bound_report_draft_v1，"
        f"content_sha256=sha256:{'b' * 64}，size_bytes=4096，current=true，"
        "publication=not_published，delivery=not_delivered。DOCX Artifact 已生成并登记。"
    )
    output = f"DOCX Artifact 已生成：artifact_id={artifact_id}。"
    result = _result("生成 ReportDraft v1 DOCX", output)
    result["messages"] = [
        HumanMessage(content="生成 ReportDraft v1 DOCX"),
        AIMessage(
            content="",
            tool_calls=[{
                "name": "render_report_docx_artifact",
                "args": {"expected_version": 1},
                "id": "call_artifact",
                "type": "tool_call",
            }],
        ),
        ToolMessage(
            content=tool_output,
            name="render_report_docx_artifact",
            tool_call_id="call_artifact",
        ),
        AIMessage(content=output),
    ]

    assert enforce_m2_output_guard(
        result,
        output,
        event_sink=lambda *_args, **_kwargs: None,
    ) == output


def test_report_artifact_claim_rejects_mismatched_ledger_id() -> None:
    result = _result("查看当前工作状态", "DOCX Artifact 已生成：artifact_id=artifact_fake。")
    result["current_work"]["report_artifacts"] = [{
        "artifact_id": "artifact_real",
        "report_draft_version": 1,
        "current": True,
    }]
    output = "DOCX Artifact 已生成：artifact_id=artifact_fake。"

    assert enforce_m2_output_guard(
        result,
        output,
        event_sink=lambda *_args, **_kwargs: None,
    ) == REPORT_ARTIFACT_LEDGER_CLAIM_BLOCKED_MESSAGE


def test_report_publication_or_delivery_claim_is_blocked_without_a_delivery_ledger() -> None:
    output = "报告已经发布并交付给员工。"

    assert enforce_m2_output_guard(
        _result("发布报告", output),
        output,
        event_sink=lambda *_args, **_kwargs: None,
    ) == REPORT_PUBLICATION_LEDGER_CLAIM_BLOCKED_MESSAGE


def test_ledger_backed_report_publication_claim_is_allowed() -> None:
    publication_id = f"publication_{'a' * 64}"
    artifact_id = f"artifact_{'b' * 64}"
    tool_output = (
        f"ReportPublication publication_id={publication_id}，publication_v1，status=published，"
        f"artifact_id={artifact_id}，bound_report_draft_v1，content_sha256=sha256:{'c' * 64}，"
        "current=true，delivery=not_delivered。"
    )
    output = "ReportArtifact v1 已正式发布，但尚未交付。"
    result = _result("发布 ReportArtifact v1", output)
    result["messages"] = [
        HumanMessage(content="发布 ReportArtifact v1"),
        AIMessage(
            content="",
            tool_calls=[{
                "name": "publish_report_artifact",
                "args": {"expected_version": 1},
                "id": "call_publication",
                "type": "tool_call",
            }],
        ),
        ToolMessage(
            content=tool_output,
            name="publish_report_artifact",
            tool_call_id="call_publication",
        ),
        AIMessage(content=output),
    ]

    assert enforce_m2_output_guard(
        result,
        output,
        event_sink=lambda *_args, **_kwargs: None,
    ) == output


def test_stale_publication_cannot_be_claimed_as_current() -> None:
    output = "ReportArtifact v1 仍已正式发布。"
    result = _result("查看当前发布状态", output)
    result["current_work"]["report_publications"] = [{
        "publication_version": 1,
        "status": "published",
        "report_draft_version": 1,
        "current": False,
    }]

    assert enforce_m2_output_guard(
        result,
        output,
        event_sink=lambda *_args, **_kwargs: None,
    ) == REPORT_PUBLICATION_LEDGER_CLAIM_BLOCKED_MESSAGE


def test_stale_artifact_cannot_be_described_as_current_publication_version() -> None:
    output = "历史旧版 ReportArtifact v1 仍是当前正式发布版本。"
    result = _result("查看当前发布状态", output)
    result["current_work"]["report_publications"] = [{
        "publication_version": 1,
        "status": "published",
        "report_draft_version": 1,
        "current": False,
    }]

    assert enforce_m2_output_guard(
        result,
        output,
        event_sink=lambda *_args, **_kwargs: None,
    ) == REPORT_PUBLICATION_LEDGER_CLAIM_BLOCKED_MESSAGE


def test_delivery_claim_remains_blocked_after_publication_support() -> None:
    output = "ReportArtifact v1 已交付给员工。"

    assert enforce_m2_output_guard(
        _result("交付 ReportArtifact v1", output),
        output,
        event_sink=lambda *_args, **_kwargs: None,
    ) == REPORT_ARTIFACT_LEDGER_CLAIM_BLOCKED_MESSAGE
