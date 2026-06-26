"""DeepAgents runtime for the enterprise WeCom digital employee."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agentseek_langchain import messages_spec
from agentseek_langchain.spec import InvocationContext, RunnableSpec
from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from agentseek_enterprise.memory import format_short_term_memory_for_prompt
from langchain_core.messages import SystemMessage

from enterprise_wecom_digital_employee.settings import PROJECT_ROOT, get_settings
from enterprise_wecom_digital_employee.tools import (
    call_mcp_tool,
    describe_employee_context_contract,
    list_mcp_tools,
)

SYSTEM_PROMPT = """You are an enterprise WeCom digital employee.

You receive one employee's message at a time through AgentSeek. Use employee_context when present.
For knowledge lookup and office workflows, discover and call MCP tools instead of inventing results.
Before state-changing operations, ask for confirmation unless the user's latest message already confirms the exact action.
Keep WeCom replies concise and operational.
"""


def build_agent() -> Any:
    """Build the local DeepAgents runnable."""

    settings = get_settings()
    return create_deep_agent(
        model=settings.build_model(),
        tools=[
            describe_employee_context_contract,
            list_mcp_tools,
            call_mcp_tool,
        ],
        system_prompt=SYSTEM_PROMPT,
        memory=[str(PROJECT_ROOT / "AGENTS.md")],
        skills=[str(PROJECT_ROOT / "skills")],
        backend=FilesystemBackend(root_dir=PROJECT_ROOT, virtual_mode=False),
    )


def build_spec():
    """Return the RunnableSpec loaded by AGENTSEEK_LANGCHAIN_SPEC."""

    base_spec = messages_spec(build_agent(), include_agents_md=True)

    def build_input(context: InvocationContext) -> object:
        runnable_input = base_spec.build_input(context)
        if not isinstance(runnable_input, dict):
            return runnable_input
        messages = runnable_input.get("messages")
        if not isinstance(messages, list):
            return runnable_input
        runtime_messages = _runtime_context_messages(context.state)
        if not runtime_messages:
            return runnable_input
        runnable_input = dict(runnable_input)
        runnable_input["messages"] = [*runtime_messages, *messages]
        return runnable_input

    return RunnableSpec(
        runnable=base_spec.runnable,
        build_input=build_input,
        parse_output=base_spec.parse_output,
        build_config=base_spec.build_config,
        stream_output=base_spec.stream_output,
    )


def _runtime_context_messages(state: Mapping[str, object]) -> list[SystemMessage]:
    messages: list[SystemMessage] = []
    if employee_message := _employee_context_message(state):
        messages.append(employee_message)
    if memory_message := _short_term_memory_message(state):
        messages.append(memory_message)
    return messages


def _employee_context_message(state: Mapping[str, object]) -> SystemMessage | None:
    context = state.get("employee_context")
    if isinstance(context, Mapping):
        lines = ["[EmployeeContext]", "员工身份已由 AgentSeek runtime 解析，回答“我是谁”时必须优先使用以下信息。"]
        for key, label in (
            ("name", "姓名"),
            ("oa_account", "OA账号"),
            ("user_id", "员工ID"),
            ("belong_to_label", "组织主体"),
            ("primary_org_name", "一级组织"),
            ("org_path_label", "组织路径"),
            ("role_label", "角色"),
            ("dept_name", "部门"),
            ("post", "岗位"),
        ):
            value = _clean(context.get(key))
            if value:
                lines.append(f"{label}: {value}")
        return SystemMessage(content="\n".join(lines))

    identity = state.get("_employee_identity")
    if isinstance(identity, Mapping):
        status = _clean(identity.get("status"))
        oa_account = _clean(identity.get("oa_account"))
        if status:
            lines = ["[EmployeeContext]", f"员工身份状态: {status}"]
            if oa_account:
                lines.append(f"查询OA账号: {oa_account}")
            lines.append("如果用户问“我是谁”，说明身份未完整解析，不要编造员工信息。")
            return SystemMessage(content="\n".join(lines))

    return None


def _short_term_memory_message(state: Mapping[str, object]) -> SystemMessage | None:
    content = format_short_term_memory_for_prompt(state.get("short_term_memory"))
    if not content:
        return None
    return SystemMessage(content=content)


def _clean(value: object) -> str:
    return str(value or "").strip()
