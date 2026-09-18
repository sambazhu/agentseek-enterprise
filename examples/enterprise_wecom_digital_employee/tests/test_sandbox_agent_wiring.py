from types import SimpleNamespace

import pytest

from enterprise_wecom_digital_employee import agent as module
from enterprise_wecom_digital_employee.sandbox_tools import sandbox_business_tools


def test_default_agent_disabled_and_explicit_tools_registered(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTSEEK_MODEL", "openai:qwen-flash")
    monkeypatch.setenv("AGENTSEEK_MODEL_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("OPENAI_API_BASE", "https://example.invalid/v1")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_STORE_SQLITE_PATH", str(tmp_path / "store.sqlite"))
    monkeypatch.setenv("AGENTSEEK_WORK_ENABLED", "false")
    module.get_settings.cache_clear()
    tools = sandbox_business_tools(resolve=lambda *args: None, backend_for=lambda *args: None)
    try:
        assert "run_sandbox_task" not in module.build_agent().nodes["tools"].bound.tools_by_name
        assert "run_sandbox_task" in module.build_agent(sandbox_tools=tools).nodes["tools"].bound.tools_by_name
    finally:
        module.get_settings.cache_clear()


@pytest.mark.parametrize("grants,enabled", [((), False), (("run_sandbox_task",), True)])
def test_profile_capability_required_with_work_registry(monkeypatch, grants, enabled):
    calls = []
    monkeypatch.setattr(module, "build_agent", lambda **kwargs: calls.append(kwargs) or object())
    monkeypatch.setattr(module, "RoutedAgentRunnable", lambda **kwargs: kwargs)
    registry = SimpleNamespace(profile=SimpleNamespace(tool_grants=grants),
        shared_capability_tools=lambda: [], playbook_refs=("report",), get=lambda ref: object())
    tools = [object()]
    module._build_runtime_runnable(registry, sandbox_tools=tools)
    assert len(calls) == 2
    assert all((c.get("sandbox_tools") == tools) is enabled for c in calls)
