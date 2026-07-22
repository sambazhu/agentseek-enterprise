from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import enterprise_wecom_digital_employee.agent as agent_module
from agentseek_langchain.spec import InvocationContext, RunnableSpec


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
    assert "This wording rule applies only to your guidance" in agent_module.SYSTEM_PROMPT
    assert "The server-side confirmation parser is the sole authority" in agent_module.SYSTEM_PROMPT


def test_routed_agent_invokes_only_the_selected_playbook(monkeypatch) -> None:
    calls: list[object] = []

    async def fake_invoke(runnable, runnable_input, config, *, runtime_context=None):
        del runnable_input, config, runtime_context
        calls.append(runnable)
        return f"result:{runnable}"

    monkeypatch.setattr(agent_module, "invoke_runnable", fake_invoke)
    routed = agent_module.RoutedAgentRunnable(
        direct="direct-agent",
        by_playbook={
            "securities-industry-report@1": "report-agent",
            "department-summary-test@1": "summary-agent",
        },
    )

    async def run():
        selected = await routed.ainvoke({
            "playbook_route": {"playbook_ref": "department-summary-test@1"}
        })
        direct = await routed.ainvoke({"playbook_route": {"playbook_ref": "unknown@1"}})
        return selected, direct

    selected, direct = asyncio.run(run())

    assert selected.playbook_ref == "department-summary-test@1"
    assert selected.value == "result:summary-agent"
    assert direct.playbook_ref is None
    assert direct.value == "result:direct-agent"
    assert calls == ["summary-agent", "direct-agent"]


def test_employee_context_hides_internal_employee_hash_from_employee_reply_context() -> None:
    message = agent_module._employee_context_message({
        "employee_context": {
            "name": "测试员工",
            "oa_account": "tester",
            "user_id": "17308da7990bc39bd424f4047fe9ec54",
            "dept_name": "战略发展部",
        }
    })

    assert message is not None
    assert "测试员工" in str(message.content)
    assert "员工ID" not in str(message.content)
    assert "17308da7990bc39bd424f4047fe9ec54" not in str(message.content)


def test_selected_playbook_ref_uses_public_route_contract() -> None:
    assert agent_module._selected_playbook_ref({
        "playbook_route": {"playbook_ref": "securities-industry-report@1"}
    }) == "securities-industry-report@1"
    assert agent_module._selected_playbook_ref({
        "playbook_route": {"selected_playbook_ref": "legacy-wrong-field@1"}
    }) is None


def test_spec_carries_route_from_runtime_state_into_routed_runnable_input(
    monkeypatch,
    tmp_path,
) -> None:
    base = RunnableSpec(
        runnable=object(),
        build_input=lambda _context: {"messages": []},
        parse_output=str,
    )
    registry = object()
    monkeypatch.setattr(agent_module, "get_settings", lambda: SimpleNamespace(work_enabled=True))
    monkeypatch.setattr(agent_module, "get_playbook_registry", lambda: registry)
    monkeypatch.setattr(agent_module, "_build_runtime_runnable", lambda selected: selected)
    monkeypatch.setattr(agent_module, "messages_spec", lambda *_args, **_kwargs: base)
    spec = agent_module.build_spec()
    route = {
        "route_status": "selected",
        "reason_code": "exact_action",
        "playbook_ref": "securities-industry-report@1",
        "candidate_playbook_refs": ["securities-industry-report@1"],
    }
    context = InvocationContext(
        prompt="查看当前 ReportArtifact",
        session_id="wecom:test",
        state={"playbook_route": route},
        workspace=tmp_path,
        agents_md=None,
    )

    runnable_input = spec.build_input(context)

    assert isinstance(runnable_input, dict)
    assert runnable_input["playbook_route"] == route
