from __future__ import annotations

from typing import Any

import enterprise_wecom_digital_employee.agent as agent_module


def test_enterprise_harness_disables_hidden_summary_and_default_subagent() -> None:
    profile = agent_module._ENTERPRISE_HARNESS_PROFILE

    assert profile.excluded_middleware == frozenset({"SummarizationMiddleware"})
    assert profile.general_purpose_subagent is not None
    assert profile.general_purpose_subagent.enabled is False


def test_enterprise_harness_registration_is_idempotent(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    def fake_register(key: str, profile: object) -> None:
        calls.append((key, profile))

    monkeypatch.setattr(agent_module, "register_harness_profile", fake_register)
    monkeypatch.setattr(agent_module, "_ENTERPRISE_HARNESS_REGISTERED", False)

    agent_module._register_enterprise_harness_profile()
    agent_module._register_enterprise_harness_profile()

    assert calls == [("openai", agent_module._ENTERPRISE_HARNESS_PROFILE)]


def test_built_agent_does_not_expose_default_task_tool(monkeypatch, tmp_path) -> None:
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

        assert "task" not in tools
        assert graph.nodes["model"].timeout is not None
        assert graph.nodes["model"].timeout.run_timeout == 12.5
    finally:
        agent_module.get_settings.cache_clear()
