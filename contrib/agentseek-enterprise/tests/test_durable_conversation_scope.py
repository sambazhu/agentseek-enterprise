import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from agentseek_enterprise.langgraph_store import SQLiteStore
from agentseek_enterprise.long_term_memory import employee_memory_tools
from agentseek_enterprise.runtime import (
    EnterpriseRuntimeSettings,
    enterprise_filesystem_namespace,
    enterprise_runtime_context,
)
from langchain_core.messages import HumanMessage
from langgraph.prebuilt import ToolRuntime


def runtime(store, session):
    return ToolRuntime(
        state={},
        context=enterprise_runtime_context(
            {"oa_account": "synthetic-employee"},
            session,
            settings=EnterpriseRuntimeSettings(tenant_id="synthetic", namespace_secret="test-only"),
        ),
        config={},
        stream_writer=lambda _: None,
        tool_call_id=None,
        store=store,
    )


def test_real_durable_tools_isolate_dm_and_two_groups(tmp_path):
    store = SQLiteStore(tmp_path / "memory.sqlite3")
    sessions = ["wecom:employee", "wecom:bot:group:a", "wecom:bot:group:b"]
    runs = [runtime(store, sid) for sid in sessions]
    tools = {tool.name: tool for tool in employee_memory_tools()}
    markers = ["P0DM-7731", "P0GA-4826", "P0GB-9017"]
    for run, marker in zip(runs, markers, strict=True):
        tools["remember_employee_memory"].func(memory=marker, category="work_context", runtime=run)
    for run, own in zip(runs, markers, strict=True):
        recalled = tools["recall_employee_memory"].func(runtime=run)
        assert own in recalled
        assert all(other not in recalled for other in markers if other != own)


def test_dm_keeps_legacy_namespace_and_groups_never_read_legacy_profile(tmp_path):
    store = SQLiteStore(tmp_path / "memory.sqlite3")
    dm = runtime(store, "wecom:employee")
    group = runtime(store, "wecom:bot:group:a")
    enterprise = dm.context["enterprise"]
    legacy = ("enterprise", "v1", enterprise["tenant_key"], enterprise["user_key"], "filesystem")
    assert enterprise_filesystem_namespace(dm) == legacy
    assert enterprise_filesystem_namespace(runtime(store, "wecom:other-dm-transport")) == legacy
    store.put(legacy, "/employee-profile.md", {"content": "legacy-private-value"})
    tools = {tool.name: tool for tool in employee_memory_tools()}
    assert "legacy-private-value" in tools["recall_employee_memory"].func(runtime=dm)
    assert "legacy-private-value" not in tools["recall_employee_memory"].func(runtime=group)
    assert store.get(legacy, "/employee-profile.md").value["content"] == "legacy-private-value"


@pytest.mark.parametrize("case", ["unknown", "missing_kind", "invalid_kind", "missing_group_key"])
def test_incomplete_context_cannot_fall_back_to_employee_namespace(tmp_path, case):
    run = runtime(
        SQLiteStore(tmp_path / "memory.sqlite3"), "opaque-session" if case == "unknown" else "wecom:bot:group:a"
    )
    identity = run.context["enterprise"]
    if case == "missing_kind":
        identity.pop("conversation_type", None)
    elif case == "invalid_kind":
        identity["conversation_type"] = "unexpected"
    elif case == "missing_group_key":
        identity.pop("session_key")
    with pytest.raises((TypeError, RuntimeError)):
        enterprise_filesystem_namespace(SimpleNamespace(context=run.context))


@pytest.mark.parametrize("kind", ["single", "group", "invalid"])
def test_plugin_preserves_ingress_type_for_opaque_session(monkeypatch, kind):
    from agentseek_enterprise.plugin import EnterprisePlugin

    plugin = EnterprisePlugin()
    monkeypatch.setattr(plugin, "_load_short_term_memory_state", lambda _: {})

    async def employee(_):
        return {"employee_context": {"oa_account": "synthetic-employee"}}

    monkeypatch.setattr(plugin, "_load_employee_state", employee)
    message = {"content": "question", "context": {"wecom": {"chat_type": kind}}}
    state = asyncio.run(plugin.load_state(message, "opaque-session"))
    context = state["_langgraph_runtime_context"]
    if kind == "invalid":
        with pytest.raises(RuntimeError):
            enterprise_filesystem_namespace(SimpleNamespace(context=context))
    else:
        namespace = enterprise_filesystem_namespace(SimpleNamespace(context=context))
        assert ("conversation" in namespace) == (kind == "group")
        assert context["enterprise"]["conversation_type"] == kind
        assert "opaque-session" not in str(context)


def test_conflicting_envelope_and_session_fails_closed():
    context = enterprise_runtime_context(
        {"oa_account": "synthetic"},
        "wecom:bot:group:a",
        message={"context": {"wecom": {"chat_type": "single"}}},
    )
    with pytest.raises(RuntimeError):
        enterprise_filesystem_namespace(SimpleNamespace(context=context))


def test_group_namespace_keeps_tenant_and_employee_boundaries():
    namespaces = set()
    for tenant, employee in (("one", "alice"), ("one", "bob"), ("two", "alice")):
        context = enterprise_runtime_context(
            {"oa_account": employee},
            "wecom:bot:group:a",
            settings=EnterpriseRuntimeSettings(tenant_id=tenant, namespace_secret="test-only"),
        )
        namespaces.add(enterprise_filesystem_namespace(SimpleNamespace(context=context)))
    assert len(namespaces) == 3


def test_dataclass_runtime_context_preserves_group_boundary(tmp_path):
    from agentseek_enterprise.runtime import EnterpriseRuntimeContext

    run = runtime(SQLiteStore(tmp_path / "memory.sqlite3"), "wecom:bot:group:a")
    wrapped = SimpleNamespace(context=EnterpriseRuntimeContext(enterprise=run.context["enterprise"]))
    assert enterprise_filesystem_namespace(wrapped) == enterprise_filesystem_namespace(run)
    assert "conversation" in enterprise_filesystem_namespace(wrapped)


def test_unknown_context_blocks_all_four_tools_without_store_access():
    store = MagicMock()
    run = runtime(store, "unknown-session")
    tools = {tool.name: tool for tool in employee_memory_tools()}
    for name, arguments in (
        ("recall_employee_memory", {}),
        ("remember_employee_memory", {"memory": "synthetic", "category": "work_context"}),
        ("forget_employee_memory", {"memory": "synthetic"}),
        ("compact_employee_memory", {}),
    ):
        with pytest.raises(RuntimeError):
            tools[name].func(runtime=run, **arguments)
    assert store.mock_calls == []


def test_forget_and_compact_only_change_current_group(tmp_path):
    store = SQLiteStore(tmp_path / "memory.sqlite3")
    runs = [runtime(store, sid) for sid in ("wecom:employee", "wecom:bot:group:a", "wecom:bot:group:b")]
    tools = {tool.name: tool for tool in employee_memory_tools()}
    for run in runs:
        tools["remember_employee_memory"].func(memory="shared-test-value", category="work_context", runtime=run)
    group = runs[1]
    group.state["messages"] = [HumanMessage(content="清理记忆")]
    before = [store.get(enterprise_filesystem_namespace(run), "/employee-profile.md").value for run in runs]
    tools["compact_employee_memory"].func(runtime=group)
    group.state["messages"] = [HumanMessage(content="忘记 shared-test-value")]
    tools["forget_employee_memory"].func(memory="shared-test-value", runtime=group)
    assert "shared-test-value" not in tools["recall_employee_memory"].func(runtime=group)
    for index in (0, 2):
        assert store.get(enterprise_filesystem_namespace(runs[index]), "/employee-profile.md").value == before[index]


def test_concurrent_durable_writes_stay_in_own_namespace(tmp_path):
    store = SQLiteStore(tmp_path / "memory.sqlite3")
    runs = [runtime(store, sid) for sid in ("wecom:employee", "wecom:bot:group:a", "wecom:bot:group:b")]
    tools = {tool.name: tool for tool in employee_memory_tools()}

    def turn(index):
        run = runs[index % 3]
        tools["remember_employee_memory"].func(memory=f"scope{index % 3}-fact", category="work_context", runtime=run)
        recalled = tools["recall_employee_memory"].func(runtime=run)
        assert f"scope{index % 3}-fact" in recalled
        assert all(f"scope{other}-fact" not in recalled for other in range(3) if other != index % 3)

    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(turn, range(30)))


def test_store_backend_uses_same_dynamic_conversation_namespace(tmp_path, monkeypatch):
    backend_module = pytest.importorskip("deepagents.backends.store")
    store = SQLiteStore(tmp_path / "memory.sqlite3")
    backend = backend_module.StoreBackend(store=store, namespace=enterprise_filesystem_namespace)
    runs = [runtime(store, sid) for sid in ("wecom:employee", "wecom:bot:group:a", "wecom:bot:group:b")]
    for index, run in enumerate(runs):
        monkeypatch.setattr(backend_module, "get_runtime", lambda run=run: run)
        assert backend.write("/test.txt", f"scope{index}-file").error is None
    for index, run in enumerate(runs):
        monkeypatch.setattr(backend_module, "get_runtime", lambda run=run: run)
        result = backend.read("/test.txt")
        assert result.error is None
        assert result.file_data["content"] == f"scope{index}-file"
