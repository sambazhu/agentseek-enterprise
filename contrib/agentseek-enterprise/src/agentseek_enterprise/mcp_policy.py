from __future__ import annotations

import fnmatch
import json
import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

MCPRisk = Literal["read", "write", "risky"]
MCPAction = Literal["allow", "deny", "confirm"]

_SENSITIVE_KEY_MARKERS = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "credential",
    "身份证",
    "银行卡",
    "密码",
    "密钥",
    "令牌",
)


@dataclass(frozen=True)
class MCPToolPolicy:
    action: MCPAction
    risk: MCPRisk
    reason: str = ""


@dataclass(frozen=True)
class MCPPolicySettings:
    enabled: bool = True
    default_action: Literal["allow", "deny"] = "allow"
    allowed_tools: tuple[str, ...] = ()
    denied_tools: tuple[str, ...] = ()
    write_tools: tuple[str, ...] = ()
    risky_tools: tuple[str, ...] = ()
    confirm_tools: tuple[str, ...] = ()
    require_confirmation: bool = True
    audit_enabled: bool = True
    audit_log_path: Path = Path("./runtime/mcp-audit.jsonl")
    max_audit_value_chars: int = 500

    @classmethod
    def from_env(cls, *, project_root: Path | None = None) -> MCPPolicySettings:
        root = project_root or Path.cwd()
        _load_dotenv_if_present(root / ".env")
        file_values = _read_policy_file(root)
        values = {
            "enabled": _truthy(_get("enabled", "AGENTSEEK_ENTERPRISE_MCP_POLICY_ENABLED", file_values, "true")),
            "default_action": _default_action(
                _get("default_action", "AGENTSEEK_ENTERPRISE_MCP_DEFAULT_ACTION", file_values, "allow")
            ),
            "allowed_tools": _patterns(_get("allowed_tools", "AGENTSEEK_ENTERPRISE_MCP_ALLOWLIST", file_values, "")),
            "denied_tools": _patterns(_get("denied_tools", "AGENTSEEK_ENTERPRISE_MCP_DENYLIST", file_values, "")),
            "write_tools": _patterns(_get("write_tools", "AGENTSEEK_ENTERPRISE_MCP_WRITE_TOOLS", file_values, "")),
            "risky_tools": _patterns(_get("risky_tools", "AGENTSEEK_ENTERPRISE_MCP_RISKY_TOOLS", file_values, "")),
            "confirm_tools": _patterns(
                _get("confirm_tools", "AGENTSEEK_ENTERPRISE_MCP_CONFIRM_TOOLS", file_values, "")
            ),
            "require_confirmation": _truthy(
                _get("require_confirmation", "AGENTSEEK_ENTERPRISE_MCP_REQUIRE_CONFIRMATION", file_values, "true")
            ),
            "audit_enabled": _truthy(
                _get("audit_enabled", "AGENTSEEK_ENTERPRISE_MCP_AUDIT_ENABLED", file_values, "true")
            ),
            "audit_log_path": _resolve_path(
                _get(
                    "audit_log_path",
                    "AGENTSEEK_ENTERPRISE_MCP_AUDIT_LOG_PATH",
                    file_values,
                    "./runtime/mcp-audit.jsonl",
                ),
                root,
            ),
            "max_audit_value_chars": max(
                64,
                int(_get("max_audit_value_chars", "AGENTSEEK_ENTERPRISE_MCP_AUDIT_MAX_VALUE_CHARS", file_values, "500")),
            ),
        }
        return cls(**values)


class MCPPolicy:
    def __init__(self, settings: MCPPolicySettings) -> None:
        self.settings = settings

    def evaluate(self, server_name: str, tool_name: str, *, confirmed: bool = False) -> MCPToolPolicy:
        if not self.settings.enabled:
            return MCPToolPolicy(action="allow", risk="read", reason="policy disabled")

        tool_ref = normalize_tool_ref(server_name, tool_name)
        if _matches(tool_ref, self.settings.denied_tools):
            return MCPToolPolicy(action="deny", risk=self.risk_for(server_name, tool_name), reason="tool denied by policy")

        if self.settings.allowed_tools and not _matches(tool_ref, self.settings.allowed_tools):
            return MCPToolPolicy(action="deny", risk=self.risk_for(server_name, tool_name), reason="tool not in allowlist")

        if self.settings.default_action == "deny" and not _matches(tool_ref, self.settings.allowed_tools):
            return MCPToolPolicy(action="deny", risk=self.risk_for(server_name, tool_name), reason="default action is deny")

        risk = self.risk_for(server_name, tool_name)
        needs_confirmation = _matches(tool_ref, self.settings.confirm_tools) or (
            self.settings.require_confirmation and risk in {"write", "risky"}
        )
        if needs_confirmation and not confirmed:
            return MCPToolPolicy(action="confirm", risk=risk, reason="explicit confirmation required")
        return MCPToolPolicy(action="allow", risk=risk, reason="allowed by policy")

    def risk_for(self, server_name: str, tool_name: str) -> MCPRisk:
        tool_ref = normalize_tool_ref(server_name, tool_name)
        if _matches(tool_ref, self.settings.risky_tools):
            return "risky"
        if _matches(tool_ref, self.settings.write_tools):
            return "write"
        return "read"

    def describe(self, server_name: str, tool_name: str) -> str:
        policy = self.evaluate(server_name, tool_name, confirmed=False)
        if policy.action == "confirm":
            return f"risk={policy.risk}, policy=confirmation_required"
        return f"risk={policy.risk}, policy={policy.action}"

    def audit(
        self,
        *,
        server_name: str,
        tool_name: str,
        action: str,
        risk: MCPRisk,
        arguments: Mapping[str, Any] | None = None,
        confirmed: bool = False,
        reason: str = "",
        result: str | None = None,
        error: BaseException | None = None,
    ) -> None:
        if not self.settings.audit_enabled:
            return
        event = {
            "timestamp": datetime.now(UTC).isoformat(),
            "server_name": server_name,
            "tool_name": tool_name,
            "tool_ref": normalize_tool_ref(server_name, tool_name),
            "action": action,
            "risk": risk,
            "confirmed": bool(confirmed),
            "reason": reason,
            "arguments": redact_value(arguments or {}, max_chars=self.settings.max_audit_value_chars),
        }
        if result is not None:
            event["result_summary"] = _truncate(result, self.settings.max_audit_value_chars)
        if error is not None:
            event["error_type"] = type(error).__name__
            event["error"] = _truncate(str(error), self.settings.max_audit_value_chars)
        self.settings.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.settings.audit_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def normalize_tool_ref(server_name: str, tool_name: str) -> str:
    return f"{str(server_name).strip()}/{str(tool_name).strip()}"


def confirmation_required_message(server_name: str, tool_name: str, policy: MCPToolPolicy) -> str:
    return (
        "MCP tool call requires explicit employee confirmation before execution.\n"
        f"- Tool: {normalize_tool_ref(server_name, tool_name)}\n"
        f"- Risk: {policy.risk}\n"
        "- Required next step: summarize the exact business action and key arguments to the employee, "
        "then wait for a clear confirmation in the latest user message.\n"
        "- After confirmation, call the same tool again with confirmed=true."
    )


def redact_value(value: Any, *, max_chars: int = 500) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _sensitive_key(key_text):
                redacted[key_text] = "[REDACTED]"
            else:
                redacted[key_text] = redact_value(item, max_chars=max_chars)
        return redacted
    if isinstance(value, list):
        return [redact_value(item, max_chars=max_chars) for item in value]
    if isinstance(value, tuple):
        return [redact_value(item, max_chars=max_chars) for item in value]
    if isinstance(value, str):
        return _truncate(value, max_chars)
    return value


def _read_policy_file(project_root: Path) -> dict[str, Any]:
    raw_path = os.environ.get("AGENTSEEK_ENTERPRISE_MCP_POLICY_PATH", "").strip()
    if not raw_path:
        return {}
    path = _resolve_path(raw_path, project_root)
    if not path.exists():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise RuntimeError("MCP policy file must contain a JSON object")
    return loaded


def _get(key: str, env_name: str, values: Mapping[str, Any], default: str) -> Any:
    if env_name in os.environ:
        return os.environ[env_name]
    return values.get(key, default)


def _patterns(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return tuple(_normalize_pattern(item) for item in parsed if str(item).strip())
        return tuple(_normalize_pattern(item) for item in text.replace("\n", ",").split(",") if item.strip())
    if isinstance(value, list):
        return tuple(_normalize_pattern(item) for item in value if str(item).strip())
    raise TypeError("MCP policy patterns must be a string or list")


def _normalize_pattern(value: Any) -> str:
    text = str(value).strip()
    if ":" in text and "/" not in text:
        server, tool = text.split(":", maxsplit=1)
        return f"{server.strip()}/{tool.strip()}"
    return text


def _matches(tool_ref: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(tool_ref, pattern) for pattern in patterns)


def _default_action(value: Any) -> Literal["allow", "deny"]:
    text = str(value or "allow").strip().lower()
    if text in {"allow", "deny"}:
        return cast(Literal["allow", "deny"], text)
    raise ValueError("AGENTSEEK_ENTERPRISE_MCP_DEFAULT_ACTION must be allow or deny")


def _resolve_path(value: Any, project_root: Path) -> Path:
    path = Path(str(value or "").strip() or "./runtime/mcp-audit.jsonl").expanduser()
    if path.is_absolute():
        return path
    return project_root / path


def _sensitive_key(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    return any(marker in lowered for marker in _SENSITIVE_KEY_MARKERS)


def _truncate(value: str, max_chars: int) -> str:
    text = str(value)
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return text[:max_chars]
    return text[: max_chars - 3].rstrip() + "..."


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _load_dotenv_if_present(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        try:
            parsed = shlex.split(value, comments=False, posix=True)
            os.environ[key] = parsed[0] if parsed else ""
        except ValueError:
            os.environ[key] = value.strip().strip("'\"")
