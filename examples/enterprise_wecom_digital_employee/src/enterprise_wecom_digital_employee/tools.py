"""Local tools for enterprise WeCom DeepAgents."""

from __future__ import annotations

import json
from typing import Any

from fastmcp import Client

from enterprise_wecom_digital_employee.settings import get_settings


def describe_employee_context_contract() -> str:
    """Describe the employee context fields available in AgentSeek runtime state."""

    return (
        "Runtime may provide employee_context with fields such as name, oa_account, "
        "primary_org_name, org_path_label, dept_name, post, belong_to_label, and role_label. "
        "Use it as identity context, not as final authorization."
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
        for tool in tools:
            description = getattr(tool, "description", "") or ""
            lines.append(f"  - {getattr(tool, 'name', '')}: {description}")
    return "\n".join(lines)


async def call_mcp_tool(server_name: str, tool_name: str, arguments: dict[str, Any] | None = None) -> str:
    """Call a configured MCP tool by server name and remote tool name."""

    servers = _read_mcp_servers()
    server_config = servers.get(server_name)
    if server_config is None:
        return f"MCP server {server_name!r} is not configured."

    async with Client({server_name: _normalize_server_config(server_config)}, init_timeout=20) as client:
        result = await client.call_tool(tool_name, arguments or {})
    return _format_mcp_result(result)


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
