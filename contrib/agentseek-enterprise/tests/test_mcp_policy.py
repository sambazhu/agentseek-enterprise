from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentseek_enterprise.mcp_policy import (
    MCPPolicy,
    MCPPolicySettings,
    confirmation_required_message,
    normalize_tool_ref,
    redact_value,
)


def test_normalize_tool_ref() -> None:
    assert normalize_tool_ref(" office ", " book_room ") == "office/book_room"


def test_policy_defaults_allow_read_tools() -> None:
    policy = MCPPolicy(MCPPolicySettings())

    decision = policy.evaluate("gildata", "FinQuery")

    assert decision.action == "allow"
    assert decision.risk == "read"


def test_policy_denies_tools_not_in_allowlist() -> None:
    policy = MCPPolicy(MCPPolicySettings(allowed_tools=("gildata/*",)))

    allowed = policy.evaluate("gildata", "FinQuery")
    denied = policy.evaluate("office", "book_room")

    assert allowed.action == "allow"
    assert denied.action == "deny"
    assert denied.reason == "tool not in allowlist"


def test_policy_requires_confirmation_for_write_tools() -> None:
    policy = MCPPolicy(MCPPolicySettings(write_tools=("office/book_room",)))

    first = policy.evaluate("office", "book_room", confirmed=False)
    confirmed = policy.evaluate("office", "book_room", confirmed=True)

    assert first.action == "confirm"
    assert first.risk == "write"
    assert confirmed.action == "allow"
    assert confirmed.risk == "write"
    assert "confirmed=true" in confirmation_required_message("office", "book_room", first)


def test_policy_requires_confirmation_for_explicit_confirm_tools() -> None:
    policy = MCPPolicy(MCPPolicySettings(confirm_tools=("gildata/FinQuery",)))

    first = policy.evaluate("gildata", "FinQuery", confirmed=False)
    confirmed = policy.evaluate("gildata", "FinQuery", confirmed=True)

    assert first.action == "confirm"
    assert first.risk == "read"
    assert confirmed.action == "allow"


def test_policy_file_and_env_values(monkeypatch: Any, tmp_path: Path) -> None:
    policy_path = tmp_path / "mcp-policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "allowed_tools": ["gildata/*"],
                "write_tools": ["office/book_room"],
                "audit_log_path": "runtime/policy-audit.jsonl",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_MCP_POLICY_PATH", str(policy_path))
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_MCP_ALLOWLIST", "office/*")

    settings = MCPPolicySettings.from_env(project_root=tmp_path)
    policy = MCPPolicy(settings)

    assert settings.audit_log_path == tmp_path / "runtime/policy-audit.jsonl"
    assert policy.evaluate("gildata", "FinQuery").action == "deny"
    assert policy.evaluate("office", "book_room").action == "confirm"


def test_audit_writes_redacted_jsonl(tmp_path: Path) -> None:
    audit_path = tmp_path / "mcp-audit.jsonl"
    policy = MCPPolicy(MCPPolicySettings(audit_log_path=audit_path))

    policy.audit(
        server_name="office",
        tool_name="book_room",
        action="succeeded",
        risk="write",
        arguments={"room": "A101", "api_token": "secret-token"},
        confirmed=True,
        result="Booked room A101",
    )

    event = json.loads(audit_path.read_text(encoding="utf-8"))
    assert event["tool_ref"] == "office/book_room"
    assert event["arguments"]["room"] == "A101"
    assert event["arguments"]["api_token"] == "[REDACTED]"
    assert event["result_summary"] == "Booked room A101"


def test_redact_value_truncates_and_redacts_nested_values() -> None:
    redacted = redact_value(
        {
            "outer": {"password": "abc"},
            "long": "x" * 20,
        },
        max_chars=8,
    )

    assert redacted["outer"]["password"] == "[REDACTED]"
    assert redacted["long"] == "xxxxx..."
