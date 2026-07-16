from __future__ import annotations

from typing import Any

import enterprise_wecom_digital_employee.agent as agent_module


def _tool_name(tool: Any) -> str:
    return str(getattr(tool, "name", getattr(tool, "__name__", "")))


def test_enterprise_harness_disables_hidden_summary_without_overriding_default_subagent() -> None:
    profile = agent_module._ENTERPRISE_HARNESS_PROFILE

    assert profile.excluded_middleware == frozenset({"SummarizationMiddleware"})
    assert profile.general_purpose_subagent is None


def test_enterprise_harness_registration_is_idempotent(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    def fake_register(key: str, profile: object) -> None:
        calls.append((key, profile))

    monkeypatch.setattr(agent_module, "register_harness_profile", fake_register)
    monkeypatch.setattr(agent_module, "_ENTERPRISE_HARNESS_REGISTERED", False)

    agent_module._register_enterprise_harness_profile()
    agent_module._register_enterprise_harness_profile()

    assert calls == [("openai", agent_module._ENTERPRISE_HARNESS_PROFILE)]


def test_built_agent_keeps_default_task_tool_and_model_node_timeout(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(agent_module, "_ENTERPRISE_HARNESS_REGISTERED", False)
    monkeypatch.setenv("AGENTSEEK_MODEL", "openai:qwen-flash")
    monkeypatch.setenv("AGENTSEEK_MODEL_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_BASE", "https://example.invalid/v1")
    monkeypatch.setenv("AGENTSEEK_MODEL_REQUEST_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_STORE_SQLITE_PATH", str(tmp_path / "store.sqlite3"))
    monkeypatch.setenv("AGENTSEEK_WORK_ENABLED", "false")
    agent_module.get_settings.cache_clear()

    try:
        graph: Any = agent_module.build_agent()
        tools = graph.nodes["tools"].bound.tools_by_name

        assert "task" in tools
        assert graph.nodes["model"].timeout is not None
        assert graph.nodes["model"].timeout.run_timeout == 12.5
    finally:
        agent_module.get_settings.cache_clear()


def test_profile_grants_remove_raw_mcp_dispatch_and_gate_file_analysis() -> None:
    granted = agent_module._direct_capability_tools(
        tool_grants=("analyze_file", "department-knowledge-read", "gildata-read"),
    )
    granted_names = {_tool_name(tool) for tool in granted}

    assert "analyze_file" in granted_names
    assert "list_mcp_tools" not in granted_names
    assert "call_mcp_tool" not in granted_names

    denied = agent_module._direct_capability_tools(tool_grants=("department-knowledge-read",))

    assert denied == []


def test_legacy_mode_keeps_generic_mcp_adapter_for_backward_compatibility() -> None:
    tools = agent_module._direct_capability_tools(tool_grants=None)
    names = {_tool_name(tool) for tool in tools}

    assert {"list_mcp_tools", "call_mcp_tool", "analyze_file"} <= names


def test_industry_report_prompt_refuses_unrelated_weather_and_forbids_mcp_name_invention() -> None:
    assert "Never invent or reconstruct an MCP server name" in agent_module.SYSTEM_PROMPT
    assert "unrelated personal utility requests such as weather" in agent_module.SYSTEM_PROMPT
