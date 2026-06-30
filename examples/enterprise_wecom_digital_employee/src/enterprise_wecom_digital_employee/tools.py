"""Local tools for enterprise WeCom DeepAgents."""

from __future__ import annotations

import json
from typing import Any

from agentseek_enterprise.mcp_policy import MCPPolicy, MCPPolicySettings, confirmation_required_message
from fastmcp import Client

from enterprise_wecom_digital_employee.settings import PROJECT_ROOT, get_settings


def describe_employee_context_contract() -> str:
    """Describe the employee context fields available in AgentSeek runtime state."""

    return (
        "Runtime may provide employee_context with fields such as name, oa_account, "
        "primary_org_name, org_path_label, dept_name, post, belong_to_label, and role_label. "
        "Runtime may also provide short_term_memory.recent_messages for same-session follow-ups. "
        "Use these as context, not as final authorization."
    )


async def list_mcp_tools() -> str:
    """List tools exposed by configured MCP servers."""

    servers = _read_mcp_servers()
    if not servers:
        return "No MCP servers configured. Add servers to .agents/mcp.json and restart the gateway."

    lines: list[str] = ["Configured MCP tools:"]
    for server_name, server_config in servers.items():
        try:
            async with Client({server_name: _normalize_server_config(server_config)}, init_timeout=20) as client:
                tools = await client.list_tools()
        except Exception as exc:
            lines.append(f"- {server_name}: not connected ({type(exc).__name__}: {exc})")
            continue

        if not tools:
            lines.append(f"- {server_name}: connected, no tools")
            continue
        lines.append(f"- {server_name}:")
        policy = _mcp_policy()
        for tool in tools:
            tool_name = getattr(tool, "name", "") or ""
            description = getattr(tool, "description", "") or ""
            lines.append(f"  - {tool_name}: {description} [{policy.describe(server_name, tool_name)}]")
    return "\n".join(lines)


async def call_mcp_tool(
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    confirmed: bool = False,
) -> str:
    """Call a configured MCP tool by server name and remote tool name.

    For tools marked as write or risky by enterprise MCP policy, call first with
    confirmed=false to receive the required confirmation prompt. Call again with
    confirmed=true only after the employee clearly confirms the exact action and
    key arguments in the latest message.
    """

    servers = _read_mcp_servers()
    server_config = servers.get(server_name)
    if server_config is None:
        return f"MCP server {server_name!r} is not configured."

    call_arguments = arguments or {}
    policy = _mcp_policy()
    decision = policy.evaluate(server_name, tool_name, confirmed=confirmed)
    if decision.action == "deny":
        policy.audit(
            server_name=server_name,
            tool_name=tool_name,
            action="denied",
            risk=decision.risk,
            arguments=call_arguments,
            confirmed=confirmed,
            reason=decision.reason,
        )
        return f"MCP tool {server_name}/{tool_name} is denied by enterprise policy: {decision.reason}."
    if decision.action == "confirm":
        policy.audit(
            server_name=server_name,
            tool_name=tool_name,
            action="confirmation_required",
            risk=decision.risk,
            arguments=call_arguments,
            confirmed=confirmed,
            reason=decision.reason,
        )
        return confirmation_required_message(server_name, tool_name, decision)

    try:
        async with Client({server_name: _normalize_server_config(server_config)}, init_timeout=20) as client:
            result = await client.call_tool(tool_name, call_arguments)
    except Exception as exc:
        policy.audit(
            server_name=server_name,
            tool_name=tool_name,
            action="failed",
            risk=decision.risk,
            arguments=call_arguments,
            confirmed=confirmed,
            reason=decision.reason,
            error=exc,
        )
        raise
    formatted = _format_mcp_result(result)
    policy.audit(
        server_name=server_name,
        tool_name=tool_name,
        action="succeeded",
        risk=decision.risk,
        arguments=call_arguments,
        confirmed=confirmed,
        reason=decision.reason,
        result=formatted,
    )
    return formatted


def _read_mcp_servers() -> dict[str, Any]:
    config_path = get_settings().resolved_mcp_config_path()
    if not config_path.exists():
        return {}
    loaded = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise RuntimeError("MCP config file must contain a JSON object")
    servers = loaded.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise RuntimeError("MCP config file must contain a mcpServers object")
    return servers


def _normalize_server_config(server_config: Any) -> dict[str, Any]:
    if not isinstance(server_config, dict):
        raise RuntimeError("MCP server config must be an object")
    normalized = dict(server_config)
    if "serverURL" in normalized and "url" not in normalized:
        normalized["url"] = normalized.pop("serverURL")
    if normalized.get("type") == "http":
        normalized["type"] = "streamable_http"
    return normalized


def _mcp_policy() -> MCPPolicy:
    return MCPPolicy(MCPPolicySettings.from_env(project_root=PROJECT_ROOT))


def _format_mcp_result(result: Any) -> str:
    structured = getattr(result, "structuredContent", None)
    content = getattr(result, "content", []) or []
    blocks: list[str] = []

    for item in content:
        item_type = getattr(item, "type", None)
        if item_type == "text":
            text = getattr(item, "text", "")
            if isinstance(text, str) and text:
                blocks.append(text)
        elif item_type == "resource":
            resource = getattr(item, "resource", None)
            text = getattr(resource, "text", None)
            if isinstance(text, str) and text:
                blocks.append(text)
        elif item_type in {"image", "audio"}:
            mime_type = getattr(item, "mimeType", "application/octet-stream")
            blocks.append(f"[Binary content: {item_type} {mime_type}]")

    if blocks:
        return "\n".join(blocks).strip()
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False, indent=2, sort_keys=True)
    return "ok"
