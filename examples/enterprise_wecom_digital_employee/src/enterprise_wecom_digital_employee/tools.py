"""Local tools for enterprise WeCom DeepAgents."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from agentseek_enterprise.mcp_policy import (
    MCPConfirmationGuard,
    MCPPolicy,
    MCPPolicySettings,
    confirmation_required_message,
)
from agentseek_enterprise.runtime import ENTERPRISE_RUNTIME_CONTEXT_KEY
from fastmcp import Client
from langchain_core.messages import BaseMessage
from langgraph.prebuilt import ToolRuntime

from enterprise_wecom_digital_employee.settings import PROJECT_ROOT, get_settings

_MCP_CONFIRMATION_GUARD: MCPConfirmationGuard | None = None
_MCP_CONFIRMATION_GUARD_CONFIG: tuple[int, int] | None = None
_CONFIRMATION_RE = re.compile(
    r"^\s*(确认|同意|可以|好的|好|是|是的|没问题|执行|继续)(执行|提交|调用|搜索|处理|吧|。|！|!|，|,|\s|$).*"
    r"|^\s*(yes|y|ok|okay|confirm|confirmed|proceed)\b.*",
    re.IGNORECASE,
)


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
    runtime: ToolRuntime,
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
    effective_confirmed = confirmed
    confirmation_status_reason = ""
    preflight_decision = policy.evaluate(server_name, tool_name, confirmed=False)
    if policy.settings.confirmation_state_enabled and preflight_decision.action == "confirm":
        confirmation_status = _mcp_confirmation_guard(policy.settings).require_or_consume(
            session_id=_runtime_confirmation_session_id(runtime),
            server_name=server_name,
            tool_name=tool_name,
            arguments=call_arguments,
            requested_confirmed=confirmed,
            user_confirmed=_latest_user_message_confirms(runtime),
        )
        effective_confirmed = confirmation_status.confirmed
        if not confirmation_status.confirmed:
            confirmation_status_reason = confirmation_status.reason

    decision = policy.evaluate(server_name, tool_name, confirmed=effective_confirmed)
    if confirmation_status_reason and decision.action == "confirm":
        decision = replace(decision, reason=f"{decision.reason}; {confirmation_status_reason}")
    if decision.action == "deny":
        policy.audit(
            server_name=server_name,
            tool_name=tool_name,
            action="denied",
            risk=decision.risk,
            arguments=call_arguments,
            confirmed=effective_confirmed,
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
            confirmed=effective_confirmed,
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
            confirmed=effective_confirmed,
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
        confirmed=effective_confirmed,
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


def _mcp_confirmation_guard(settings: MCPPolicySettings) -> MCPConfirmationGuard:
    global _MCP_CONFIRMATION_GUARD, _MCP_CONFIRMATION_GUARD_CONFIG
    config = (settings.confirmation_ttl_seconds, settings.confirmation_max_pending)
    if _MCP_CONFIRMATION_GUARD is None or config != _MCP_CONFIRMATION_GUARD_CONFIG:
        _MCP_CONFIRMATION_GUARD = MCPConfirmationGuard(
            ttl_seconds=settings.confirmation_ttl_seconds,
            max_pending=settings.confirmation_max_pending,
        )
        _MCP_CONFIRMATION_GUARD_CONFIG = config
    return _MCP_CONFIRMATION_GUARD


def _runtime_confirmation_session_id(runtime: ToolRuntime | None) -> str:
    if runtime is None:
        return ""

    context = getattr(runtime, "context", None)
    enterprise = _mapping_get(context, ENTERPRISE_RUNTIME_CONTEXT_KEY)
    session_key = _clean(_mapping_get(enterprise, "session_key"))
    if session_key:
        return f"enterprise:{session_key}"

    config = getattr(runtime, "config", None)
    configurable = _mapping_get(config, "configurable")
    for key in ("thread_id", "session_id"):
        value = _clean(_mapping_get(configurable, key))
        if value:
            return value

    metadata = _mapping_get(config, "metadata")
    session_id = _clean(_mapping_get(metadata, "session_id"))
    if session_id:
        return session_id

    execution_info = getattr(runtime, "execution_info", None)
    thread_id = _clean(getattr(execution_info, "thread_id", None))
    if thread_id:
        return thread_id

    state = getattr(runtime, "state", None)
    for key in ("session_id", "sessionid"):
        value = _clean(_mapping_get(state, key))
        if value:
            return value
    return ""


def _latest_user_message_confirms(runtime: ToolRuntime | None) -> bool:
    if runtime is None:
        return False
    state = getattr(runtime, "state", None)
    messages = _mapping_get(state, "messages")
    if not isinstance(messages, list):
        return False
    for message in reversed(messages):
        if not _is_human_message(message):
            continue
        text = _message_text(message)
        return bool(_CONFIRMATION_RE.match(text))
    return False


def _is_human_message(message: object) -> bool:
    if isinstance(message, BaseMessage):
        return message.type == "human"
    if isinstance(message, Mapping):
        role = _clean(message.get("role") or message.get("type"))
        return role in {"human", "user"}
    return False


def _message_text(message: object) -> str:
    content = getattr(message, "content", None)
    if content is None and isinstance(message, Mapping):
        content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, Mapping) and isinstance(item.get("text"), str):
                parts.append(str(item["text"]))
        return "\n".join(parts).strip()
    return _clean(content)


def _mapping_get(value: object, key: str) -> object:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _clean(value: object) -> str:
    return str(value or "").strip()


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
