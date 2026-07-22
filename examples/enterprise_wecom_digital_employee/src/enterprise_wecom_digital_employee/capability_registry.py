from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from agentseek_files.analysis_tools import file_analysis_tools
from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import ToolRuntime

from enterprise_wecom_digital_employee.capability_catalog import (
    RuntimeCapabilityAvailability,
    configured_mcp_server_names,
    resolve_runtime_capabilities,
)
from enterprise_wecom_digital_employee.channel_command import authenticated_user_command_text
from enterprise_wecom_digital_employee.pack_loader import DigitalEmployeeProfile
from enterprise_wecom_digital_employee.tools import call_mcp_tool

MCPInvoker = Callable[[str, str, dict[str, Any] | None, bool], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class CapabilityRegistry:
    """One Profile-owned capability pool shared by DirectTurn and Playbooks."""

    availability: RuntimeCapabilityAvailability
    tools: tuple[BaseTool, ...]
    invoke_mcp: MCPInvoker


def build_capability_registry(
    profile: DigitalEmployeeProfile,
    *,
    mcp_config_path: Path,
    invoke_mcp: MCPInvoker = call_mcp_tool,
) -> CapabilityRegistry:
    configured_servers = configured_mcp_server_names(mcp_config_path)
    availability = resolve_runtime_capabilities(
        profile,
        effective_tool_grants=frozenset(profile.tool_grants),
        effective_data_scopes=frozenset(profile.data_scopes),
        configured_servers=configured_servers,
    )
    return CapabilityRegistry(
        availability=availability,
        tools=tuple(_shared_tools(availability, invoke_mcp)),
        invoke_mcp=invoke_mcp,
    )


def explicitly_authorizes_external_capability(
    message: str,
    *,
    capability: Literal["licensed_data", "public_search"],
) -> bool:
    command = "".join(authenticated_user_command_text(message).lower().split())
    if not command or any(term in command for term in ("不要", "不允许", "别用", "取消")):
        return False
    action = any(term in command for term in ("使用", "查询", "搜索", "检索", "查找"))
    if capability == "licensed_data":
        source = any(term in command for term in ("gildata", "聚源", "专业数据库", "外部数据"))
    else:
        source = any(term in command for term in ("tavily", "公开搜索", "公开信息", "网络搜索", "互联网"))
    return action and source


def _shared_tools(
    availability: RuntimeCapabilityAvailability,
    invoke_mcp: MCPInvoker,
) -> list[BaseTool]:
    tools: list[BaseTool] = []
    if availability.file_analysis:
        tools.extend(file_analysis_tools())
    if availability.department_knowledge:
        tools.extend(_department_knowledge_tools(invoke_mcp))
    if availability.licensed_external_data:
        tools.append(_licensed_data_tool(invoke_mcp))
    if availability.public_search:
        tools.append(_public_search_tool(invoke_mcp))
    return tools


def _department_knowledge_tools(invoke_mcp: MCPInvoker) -> list[BaseTool]:
    @tool("list_department_knowledge")
    async def list_department_knowledge(runtime: ToolRuntime, limit: int = 20) -> str:
        """List documents in the configured department knowledge collection."""

        if refusal := _formal_workflow_refusal(runtime):
            return refusal
        return await invoke_mcp(
            "department-knowledge",
            "knowledge_list_documents",
            {"limit": limit},
            False,
        )

    @tool("search_department_knowledge")
    async def search_department_knowledge(
        query: str,
        runtime: ToolRuntime,
        search_mode: Literal["keyword", "semantic", "hybrid"] = "hybrid",
        top_k: int = 8,
    ) -> str:
        """Search configured department knowledge for ordinary employee assistance.

        In a selected formal report Playbook, use its research workflow tool so
        evidence is registered in the WorkItem ledger.
        """

        if refusal := _formal_workflow_refusal(runtime):
            return refusal
        return await invoke_mcp(
            "department-knowledge",
            "knowledge_search",
            {"query": query, "search_mode": search_mode, "top_k": top_k},
            False,
        )

    @tool("read_department_knowledge")
    async def read_department_knowledge(chunk_ids: list[str], runtime: ToolRuntime) -> str:
        """Read selected chunk IDs returned by search_department_knowledge."""

        if refusal := _formal_workflow_refusal(runtime):
            return refusal
        return await invoke_mcp(
            "department-knowledge",
            "knowledge_read_chunks",
            {"chunk_ids": chunk_ids},
            False,
        )

    return [
        list_department_knowledge,
        search_department_knowledge,
        read_department_knowledge,
    ]


def _licensed_data_tool(invoke_mcp: MCPInvoker) -> BaseTool:
    @tool("search_licensed_external_data")
    async def search_licensed_external_data(query: str, runtime: ToolRuntime) -> str:
        """Search configured licensed data after an explicit employee request.

        The latest employee message must explicitly request Gildata, JuYuan,
        licensed data, or an external database. Formal report gaps must use the
        Playbook's version-bound gap-decision workflow instead.
        """

        refusal = _direct_external_refusal(runtime, capability="licensed_data")
        if refusal:
            return refusal
        return await invoke_mcp(
            "gildata_datamap-data",
            "FinGeneralQuery",
            {"query": query},
            True,
        )

    return search_licensed_external_data


def _public_search_tool(invoke_mcp: MCPInvoker) -> BaseTool:
    @tool("search_public_information")
    async def search_public_information(
        query: str,
        runtime: ToolRuntime,
        max_results: int = 5,
    ) -> str:
        """Search configured public information after an explicit employee request.

        The latest employee message must explicitly request Tavily, public search,
        public information, web search, or internet search. Formal report gaps must
        use the Playbook's version-bound gap-decision workflow instead.
        """

        refusal = _direct_external_refusal(runtime, capability="public_search")
        if refusal:
            return refusal
        return await invoke_mcp(
            "tavily-search",
            "tavily_search",
            {"query": query, "max_results": max_results, "search_depth": "advanced"},
            True,
        )

    return search_public_information


def _direct_external_refusal(
    runtime: ToolRuntime,
    *,
    capability: Literal["licensed_data", "public_search"],
) -> str | None:
    state = runtime.state if isinstance(runtime.state, Mapping) else {}
    if refusal := _formal_workflow_refusal(runtime):
        return refusal
    latest = _latest_user_message(state)
    if explicitly_authorizes_external_capability(latest, capability=capability):
        return None
    source = "Gildata/聚源等外部数据" if capability == "licensed_data" else "公开搜索"
    return f"使用{source}前需要员工在最新消息中明确提出该检索请求。"


def _formal_workflow_refusal(runtime: ToolRuntime) -> str | None:
    state = runtime.state if isinstance(runtime.state, Mapping) else {}
    if not _selected_playbook(state):
        return None
    return (
        "当前消息已进入正式 Playbook。请使用该 Playbook 的版本化研究工具，"
        "以便登记证据、缺口决策和员工授权。"
    )


def _selected_playbook(state: Mapping[str, object]) -> bool:
    route = state.get("playbook_route")
    return isinstance(route, Mapping) and route.get("route_status") == "selected"


def _latest_user_message(state: Mapping[str, object]) -> str:
    explicit = state.get("latest_user_message")
    if isinstance(explicit, str) and explicit.strip():
        return explicit
    messages = state.get("messages")
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        return ""
    for message in reversed(messages):
        if isinstance(message, Mapping):
            role = str(message.get("role") or message.get("type") or "").lower()
            content = message.get("content")
        else:
            role = str(getattr(message, "role", "") or getattr(message, "type", "")).lower()
            content = getattr(message, "content", None)
        if role in {"human", "user"}:
            return str(content or "")
    return ""
