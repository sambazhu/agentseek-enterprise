"""Narrow client-side compatibility for purchased platform MCP contracts."""

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

OA_SERVERS = frozenset({"OA流程助手", "znsh"})
KB_SERVERS = {
    "信息技术部知识库": {"e3n5qeo1ec2w", "i4ucnjkj5b9y"},
    "战略发展部知识库": {"urzi680on449"},
    "人力资源知识库": {"z1sdxq9hme78"},
    "e3n5qeo1ec2w": {"e3n5qeo1ec2w", "i4ucnjkj5b9y"},
    "i4ucnjkj5b9y": {"i4ucnjkj5b9y"},
    "urzi680on449": {"urzi680on449"},
    "z1sdxq9hme78": {"z1sdxq9hme78"},
}


def effective_schema(server: str, tool: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Correct only the verified dangling OA required field, without mutating discovery."""
    result = deepcopy(schema)
    props = result.get("properties", {})
    if (server in OA_SERVERS and tool == "查询最近费用报销流程"
            and "oa_account" in props and "login_name" not in props):
        result["required"] = list(dict.fromkeys(
            "oa_account" if key == "login_name" else key
            for key in [*result.get("required", []), "oa_account"]
        ))
    return result


def bind_kb_arguments(server: str, schema: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    """Materialize declared defaults; pin kbCode to this remote tool's declared default."""
    from jsonschema import validate

    result = deepcopy(arguments)
    props = schema.get("properties", {})
    code = props.get("kbCode", {}).get("default")
    if not isinstance(code, str) or code not in KB_SERVERS[server]:
        raise ValueError("KB tool default binding unavailable")
    if result.get("kbCode") not in (None, "", code):
        raise ValueError("KB tool binding mismatch")
    result["kbCode"] = code
    for key, spec in props.items():
        if isinstance(spec, dict) and spec.get("default") is not None and result.get(key) is None:
            result[key] = deepcopy(spec["default"])
    if set(result) - props.keys():
        raise ValueError("KB arguments outside schema")
    validate(result, schema)
    return result


def local_transport_overrides(servers: dict[str, Any]) -> dict[str, Any]:
    """Apply operator-owned env references on read, never rewrite platform configuration."""
    path = os.environ.get("AGENTSEEK_MCP_LOCAL_OVERRIDES", "")
    if not path:
        return servers
    try:
        overrides = json.loads(Path(path).read_text(encoding="utf-8"))
        result = _apply_overrides(servers, overrides)
    except Exception:
        raise ValueError("Local MCP override configuration unavailable or invalid") from None
    return result


def _apply_overrides(servers: dict[str, Any], overrides: Any) -> dict[str, Any]:
    if not isinstance(overrides, dict):
        raise ValueError
    result = deepcopy(servers)
    for server, fields in overrides.items():
        if server not in result or not isinstance(fields, dict):
            raise ValueError
        if set(fields) - {"headers_from_env", "env_from_env"}:
            raise ValueError
        for section, refs in fields.items():
            if not isinstance(refs, dict):
                raise ValueError
            target = "headers" if section == "headers_from_env" else "env"
            for key, variable in refs.items():
                if not isinstance(variable, str) or not os.environ.get(variable):
                    raise ValueError
                result[server].setdefault(target, {})[key] = os.environ[variable]
    return result
