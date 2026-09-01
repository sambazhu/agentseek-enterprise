from __future__ import annotations

import asyncio
import threading
from collections.abc import Mapping
from contextlib import closing
from types import SimpleNamespace
from typing import Any

from agentseek_enterprise import plugin as enterprise_plugin
from agentseek_enterprise.identity import EmployeeContext
from agentseek_enterprise.langgraph_store import SQLAlchemyStore, SQLiteStore, build_langgraph_store
from agentseek_enterprise.long_term_memory import employee_memory_tools
from agentseek_enterprise.memory import (
    SHORT_TERM_MEMORY_STATE_KEY,
    ShortTermMemorySettings,
    SQLAlchemyShortTermMemoryStore,
    SQLiteShortTermMemoryStore,
    build_short_term_memory_store,
    format_short_term_memory_for_prompt,
)
from agentseek_enterprise.plugin import (
    EMPLOYEE_CONTEXT_STATE_KEY,
    EMPLOYEE_IDENTITY_STATE_KEY,
    LATEST_USER_MESSAGE_STATE_KEY,
    EnterprisePlugin,
    extract_oa_account,
    format_employee_context_for_prompt,
)
from agentseek_enterprise.runtime import (
    LANGGRAPH_RUNTIME_CONTEXT_STATE_KEY,
    EnterpriseRuntimeSettings,
    enterprise_filesystem_namespace,
    enterprise_runtime_context,
)
from bub.turn_admission import AdmitDecision, TurnSnapshot


class FakeIdentityProvider:
    def __init__(self, context: EmployeeContext | None) -> None:
        self.context = context
        self.queries: list[str] = []

    def get_employee_context(self, oa_account: str) -> EmployeeContext | None:
        self.queries.append(oa_account)
        return self.context


def _load_state(plugin: EnterprisePlugin, message: dict[str, Any], session_id: str) -> dict[str, Any]:
    return asyncio.run(plugin.load_state(message, session_id))


def _employee_context() -> EmployeeContext:
    return EmployeeContext(
        user_id="person-1",
        oa_account="chenkang2",
        name="陈康",
        dept_id="dept-1",
        dept_name="财富管理研发团队",
        primary_org_id="company",
        primary_org_name="公司总部",
        org_path=[
            {"id": "company", "no": "HQ", "name": "公司总部", "parent_id": "root", "org_type": "2"},
            {"id": "info-tech", "no": "DEPT-IT", "name": "信息技术部", "parent_id": "company", "org_type": "2"},
            {"id": "dept-1", "no": "DEPT-RD", "name": "财富管理研发团队", "parent_id": "info-tech", "org_type": "2"},
        ],
        org_path_label="公司总部 / 信息技术部 / 财富管理研发团队",
        post="软件开发岗",
        belong_to="1",
        belong_to_label="公司总部",
        role="1",
        role_label="总部员工",
    )


def test_extract_oa_account_reads_flat_and_nested_envelopes() -> None:
    assert extract_oa_account({"from_userid": " chenkang2 "}) == "chenkang2"
    assert extract_oa_account({"context": {"userid": "zhangsan"}}) == "zhangsan"
    assert extract_oa_account({"metadata": {"oa_account": "lisi"}}) == "lisi"


def test_load_state_injects_employee_context(monkeypatch: Any) -> None:
    monkeypatch.setenv("AGENTSEEK_IDENTITY_PROVIDER", "dm")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_MEMORY_ENABLED", "false")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_TENANT_ID", "wkzq")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_NAMESPACE_SECRET", "test-secret")
    plugin = EnterprisePlugin()
    provider = FakeIdentityProvider(_employee_context())
    plugin._provider = provider
    plugin._provider_initialized = True

    state = _load_state(plugin, {"from_userid": "chenkang2"}, "wecom:chenkang2")

    assert provider.queries == ["chenkang2"]
    assert state[EMPLOYEE_CONTEXT_STATE_KEY]["oa_account"] == "chenkang2"
    assert state[EMPLOYEE_CONTEXT_STATE_KEY]["belong_to_label"] == "公司总部"
    assert state[EMPLOYEE_CONTEXT_STATE_KEY]["org_path_label"] == "公司总部 / 信息技术部 / 财富管理研发团队"
    assert state[EMPLOYEE_IDENTITY_STATE_KEY]["status"] == "found"
    runtime_context = state[LANGGRAPH_RUNTIME_CONTEXT_STATE_KEY]
    assert runtime_context["enterprise"]["tenant_id"] == "wkzq"
    assert "chenkang2" not in str(runtime_context)
    assert "wecom:chenkang2" not in str(runtime_context)


def test_load_state_skips_when_identity_disabled(monkeypatch: Any) -> None:
    monkeypatch.delenv("AGENTSEEK_IDENTITY_PROVIDER", raising=False)
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_IDENTITY_ENABLED", "false")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_MEMORY_ENABLED", "false")
    plugin = EnterprisePlugin()

    assert _load_state(plugin, {"from_userid": "chenkang2"}, "s1") == {}


def test_load_state_preserves_latest_user_message_for_runtime_guards(monkeypatch: Any) -> None:
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_IDENTITY_ENABLED", "false")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_MEMORY_ENABLED", "false")
    plugin = EnterprisePlugin()

    state = _load_state(plugin, {"content": "  确认  "}, "wecom:chenkang2")

    assert state[LATEST_USER_MESSAGE_STATE_KEY] == "确认"


def test_load_state_marks_missing_employee(monkeypatch: Any) -> None:
    monkeypatch.setenv("AGENTSEEK_IDENTITY_PROVIDER", "dm")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_MEMORY_ENABLED", "false")
    plugin = EnterprisePlugin()
    plugin._provider = FakeIdentityProvider(None)
    plugin._provider_initialized = True

    state = _load_state(plugin, {"from_userid": "missing"}, "s1")

    assert EMPLOYEE_CONTEXT_STATE_KEY not in state
    assert state[EMPLOYEE_IDENTITY_STATE_KEY]["status"] == "not_found"


def test_load_state_caches_successful_employee_context(monkeypatch: Any) -> None:
    monkeypatch.setenv("AGENTSEEK_IDENTITY_PROVIDER", "dm")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_MEMORY_ENABLED", "false")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_IDENTITY_CACHE_ENABLED", "true")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_IDENTITY_CACHE_TTL_SECONDS", "600")
    plugin = EnterprisePlugin()
    provider = FakeIdentityProvider(_employee_context())
    plugin._provider = provider
    plugin._provider_initialized = True

    first = _load_state(plugin, {"from_userid": "chenkang2"}, "wecom:chenkang2")
    second = _load_state(plugin, {"from_userid": "chenkang2"}, "wecom:chenkang2")

    assert provider.queries == ["chenkang2"]
    assert first[EMPLOYEE_IDENTITY_STATE_KEY]["cache"] == "miss"
    assert second[EMPLOYEE_IDENTITY_STATE_KEY]["cache"] == "hit"
    assert second[EMPLOYEE_CONTEXT_STATE_KEY]["oa_account"] == "chenkang2"


def test_load_state_identity_cache_expires(monkeypatch: Any) -> None:
    monkeypatch.setenv("AGENTSEEK_IDENTITY_PROVIDER", "dm")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_MEMORY_ENABLED", "false")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_IDENTITY_CACHE_ENABLED", "true")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_IDENTITY_CACHE_TTL_SECONDS", "1")
    plugin = EnterprisePlugin()
    provider = FakeIdentityProvider(_employee_context())
    plugin._provider = provider
    plugin._provider_initialized = True

    _load_state(plugin, {"from_userid": "chenkang2"}, "wecom:chenkang2")
    cache_key, cached = next(iter(plugin._identity_cache.items()))
    plugin._identity_cache[cache_key] = enterprise_plugin._IdentityCacheEntry(
        context=cached.context,
        expires_at=0,
    )
    state = _load_state(plugin, {"from_userid": "chenkang2"}, "wecom:chenkang2")

    assert provider.queries == ["chenkang2", "chenkang2"]
    assert state[EMPLOYEE_IDENTITY_STATE_KEY]["cache"] == "miss"


def test_load_state_does_not_cache_missing_employee(monkeypatch: Any) -> None:
    monkeypatch.setenv("AGENTSEEK_IDENTITY_PROVIDER", "dm")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_MEMORY_ENABLED", "false")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_IDENTITY_CACHE_ENABLED", "true")
    plugin = EnterprisePlugin()
    provider = FakeIdentityProvider(None)
    plugin._provider = provider
    plugin._provider_initialized = True

    _load_state(plugin, {"from_userid": "missing"}, "s1")
    _load_state(plugin, {"from_userid": "missing"}, "s1")

    assert provider.queries == ["missing", "missing"]


def test_format_employee_context_for_prompt() -> None:
    prompt = format_employee_context_for_prompt(_employee_context().to_dict())

    assert "[EmployeeContext]" in prompt
    assert "姓名: 陈康" in prompt
    assert "组织主体: 公司总部" in prompt
    assert "组织路径: 公司总部 / 信息技术部 / 财富管理研发团队" in prompt


def test_short_term_memory_persists_recent_messages(monkeypatch: Any, tmp_path: Any) -> None:
    memory_path = tmp_path / "short-term-memory.sqlite3"
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_IDENTITY_ENABLED", "false")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_MEMORY_ENABLED", "true")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_MEMORY_SQLITE_PATH", str(memory_path))
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_MEMORY_RECENT_TURNS", "2")

    plugin = EnterprisePlugin()
    plugin.save_state(
        "wecom:chenkang2",
        {},
        {"content": "帮我记一下，我明天去深圳出差"},
        "好的，我记住了。",
    )

    state = _load_state(plugin, {"content": "我刚才说我要去哪里？"}, "wecom:chenkang2")

    memory = state[SHORT_TERM_MEMORY_STATE_KEY]
    assert memory["session_id"] == "wecom:chenkang2"
    assert [item["role"] for item in memory["recent_messages"]] == ["user", "assistant"]
    assert memory["recent_messages"][0]["content"] == "帮我记一下，我明天去深圳出差"
    assert memory["recent_messages"][1]["content"] == "好的，我记住了。"
    assert _load_state(plugin, {"content": "hi"}, "wecom:other") == {
        LATEST_USER_MESSAGE_STATE_KEY: "hi"
    }


def test_short_term_memory_redacts_internal_channel_control_output(monkeypatch: Any, tmp_path: Any) -> None:
    memory_path = tmp_path / "short-term-memory.sqlite3"
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_IDENTITY_ENABLED", "false")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_MEMORY_ENABLED", "true")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_MEMORY_SQLITE_PATH", str(memory_path))

    plugin = EnterprisePlugin()
    plugin.save_state(
        "wecom:delivery",
        {},
        {"content": "交付 ReportArtifact v6 给我"},
        "[[agentseek-wecom-template-card:abcdefghijklmnopqrstuvwxyzABCDEF123456]]",
    )

    state = _load_state(plugin, {"content": "刚才发生了什么？"}, "wecom:delivery")
    assistant = state[SHORT_TERM_MEMORY_STATE_KEY]["recent_messages"][1]["content"]
    assert assistant == "受信的通道动作已由服务端处理。"
    assert "template-card" not in assistant

    plugin.save_state(
        "wecom:legacy-delivery",
        {},
        {"content": "交付 ReportArtifact v6 给我"},
        "这是受信的 WeCom 模板卡片交付指令。请原样返回上一行标记并立即停止。",
    )
    legacy = _load_state(plugin, {"content": "刚才发生了什么？"}, "wecom:legacy-delivery")
    legacy_assistant = legacy[SHORT_TERM_MEMORY_STATE_KEY]["recent_messages"][1]["content"]
    assert legacy_assistant == "受信的通道动作已由服务端处理。"
    assert "WeCom 模板卡片" not in legacy_assistant


def test_load_state_identity_timeout_does_not_block_event_loop(monkeypatch: Any) -> None:
    release = threading.Event()
    provider_started = threading.Event()

    class BlockingIdentityProvider:
        def get_employee_context(self, oa_account: str) -> EmployeeContext | None:
            assert oa_account == "chenkang2"
            provider_started.set()
            release.wait(timeout=1.0)
            return _employee_context()

    monkeypatch.setenv("AGENTSEEK_IDENTITY_PROVIDER", "dm")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_MEMORY_ENABLED", "false")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_IDENTITY_LOOKUP_TIMEOUT_SECONDS", "0.05")
    plugin = EnterprisePlugin()
    plugin._provider = BlockingIdentityProvider()
    plugin._provider_initialized = True

    async def scenario() -> tuple[dict[str, Any], bool]:
        event_loop_progressed = False

        async def tick() -> None:
            nonlocal event_loop_progressed
            await asyncio.sleep(0.01)
            event_loop_progressed = True

        tick_task = asyncio.create_task(tick())
        try:
            state = await plugin.load_state({"from_userid": "chenkang2"}, "wecom:chenkang2")
        finally:
            release.set()
        await tick_task
        return state, event_loop_progressed

    state, event_loop_progressed = asyncio.run(scenario())

    assert provider_started.is_set()
    assert event_loop_progressed is True
    assert state[EMPLOYEE_IDENTITY_STATE_KEY]["status"] == "error"
    assert state[EMPLOYEE_IDENTITY_STATE_KEY]["error_type"] == "TimeoutError"


def test_short_term_memory_sqlite_uses_wal_and_busy_timeout(tmp_path: Any) -> None:
    store = SQLiteShortTermMemoryStore(
        ShortTermMemorySettings(
            enabled=True,
            sqlite_path=tmp_path / "short-term-memory.sqlite3",
            sqlite_busy_timeout_ms=12_345,
        )
    )

    with closing(store._connect()) as connection:
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 12_345
        assert str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"


def test_short_term_memory_sqlalchemy_store_persists_recent_messages(tmp_path: Any) -> None:
    settings = ShortTermMemorySettings(
        enabled=True,
        sqlalchemy_url=f"sqlite+pysqlite:///{tmp_path / 'short-term-memory-sa.sqlite3'}",
        recent_turns=2,
    )
    store = SQLAlchemyShortTermMemoryStore(settings)
    store.append_turn("wecom:chenkang2", "帮我记一下，我明天去深圳出差", "好的，我记住了。")

    restarted = SQLAlchemyShortTermMemoryStore(settings)
    messages = restarted.load_recent_messages("wecom:chenkang2")

    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "帮我记一下，我明天去深圳出差"
    assert restarted.load_recent_messages("wecom:other") == []


def test_short_term_memory_factory_prefers_sqlalchemy_url(tmp_path: Any) -> None:
    store = build_short_term_memory_store(
        ShortTermMemorySettings(
            enabled=True,
            sqlalchemy_url=f"sqlite+pysqlite:///{tmp_path / 'factory.sqlite3'}",
        )
    )

    assert isinstance(store, SQLAlchemyShortTermMemoryStore)


def test_system_prompt_can_include_short_term_memory(monkeypatch: Any) -> None:
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_IDENTITY_SYSTEM_PROMPT", "false")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_MEMORY_SYSTEM_PROMPT", "true")
    plugin = EnterprisePlugin()

    prompt = plugin.system_prompt(
        "",
        {
            SHORT_TERM_MEMORY_STATE_KEY: {
                "session_id": "wecom:chenkang2",
                "recent_messages": [
                    {"role": "user", "content": "帮我记一下，我明天去深圳出差"},
                    {"role": "assistant", "content": "好的，我记住了。"},
                ],
            }
        },
    )

    assert prompt is not None
    assert "[ShortTermMemory]" in prompt
    assert "历史用户原文: 帮我记一下，我明天去深圳出差" in prompt
    assert "不得改写或编造 UUID/哈希/内部 ID" in prompt
    assert "历史助手回复可能错误" in prompt


def test_short_term_memory_prompt_preserves_user_literal_over_assistant_hallucination() -> None:
    prompt = format_short_term_memory_for_prompt(
        {
            "recent_messages": [
                {"role": "user", "content": "GROUP-GAMMA-M05R2-literal"},
                {"role": "assistant", "content": "0123456789abcdef0123456789abcdef"},
            ]
        }
    )

    assert prompt is not None
    assert "历史用户原文: GROUP-GAMMA-M05R2-literal" in prompt
    assert "历史助手回复: 0123456789abcdef0123456789abcdef" in prompt
    assert "员工自带字面量不是 runtime 内部 ID" in prompt
    assert "只能从最相关的历史用户原文逐字复制" in prompt
    assert "以用户原文为准" in prompt


def test_short_term_memory_prompt_keeps_newest_messages_with_bounded_content() -> None:
    prompt = format_short_term_memory_for_prompt(
        {
            "recent_messages": [
                {"role": "user", "content": "old-" + "x" * 80},
                {"role": "assistant", "content": "middle-" + "y" * 80},
                {"role": "user", "content": "newest-" + "z" * 80},
            ]
        },
        max_chars=240,
        max_message_chars=30,
    )

    assert prompt is not None
    assert len(prompt) <= 240
    assert "newest-" in prompt
    assert "…[已截断]" in prompt
    assert "old-" not in prompt


def _turn_snapshot(
    *,
    is_running: bool = False,
    pending_count: int = 0,
    steering_count: int = 0,
    session_id: str = "wecom:userA",
) -> TurnSnapshot:
    return TurnSnapshot(
        session_id=session_id,
        is_running=is_running,
        running_count=1 if is_running else 0,
        pending_count=pending_count,
        steering_count=steering_count,
    )


def test_admit_message_returns_none_when_session_idle(monkeypatch: Any) -> None:
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_SERIALIZE_TURNS", "true")
    plugin = EnterprisePlugin()

    assert plugin.admit_message("wecom:userA", {}, _turn_snapshot(is_running=False)) is None


def test_admit_message_queues_follow_up_when_session_busy(monkeypatch: Any) -> None:
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_SERIALIZE_TURNS", "true")
    plugin = EnterprisePlugin()

    decision = plugin.admit_message("wecom:userA", {}, _turn_snapshot(is_running=True))

    assert isinstance(decision, AdmitDecision)
    assert decision.action == "follow_up"


def test_admit_message_queues_follow_up_when_pending_exists(monkeypatch: Any) -> None:
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_SERIALIZE_TURNS", "true")
    plugin = EnterprisePlugin()

    decision = plugin.admit_message("wecom:userA", {}, _turn_snapshot(pending_count=2))

    assert decision is not None
    assert decision.action == "follow_up"


def test_admit_message_serializes_by_default_when_env_unset(monkeypatch: Any) -> None:
    monkeypatch.delenv("AGENTSEEK_ENTERPRISE_SERIALIZE_TURNS", raising=False)
    plugin = EnterprisePlugin()

    decision = plugin.admit_message("wecom:userA", {}, _turn_snapshot(is_running=True))

    assert isinstance(decision, AdmitDecision)
    assert decision.action == "follow_up"


def test_admit_message_disabled_returns_none_even_when_busy(monkeypatch: Any) -> None:
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_SERIALIZE_TURNS", "false")
    plugin = EnterprisePlugin()

    assert plugin.admit_message("wecom:userA", {}, _turn_snapshot(is_running=True)) is None


def test_employee_namespace_is_stable_and_isolated() -> None:
    settings = EnterpriseRuntimeSettings(tenant_id="wkzq", namespace_secret="test-secret")
    employee = _employee_context().to_dict()
    first = enterprise_runtime_context(employee, "wecom:chenkang2", settings=settings)
    second = enterprise_runtime_context(employee, "wecom:another-session", settings=settings)
    other_employee = dict(employee, oa_account="other-user")
    third = enterprise_runtime_context(other_employee, "wecom:other-user", settings=settings)

    assert first is not None
    assert second is not None
    assert third is not None
    first_namespace = enterprise_filesystem_namespace(SimpleNamespace(context=first))
    second_namespace = enterprise_filesystem_namespace(SimpleNamespace(context=second))
    third_namespace = enterprise_filesystem_namespace(SimpleNamespace(context=third))
    assert first_namespace == second_namespace
    assert first_namespace != third_namespace
    assert "chenkang2" not in "/".join(first_namespace)


def test_sqlite_store_persists_and_isolates_namespaces(tmp_path: Any) -> None:
    path = tmp_path / "enterprise-store.sqlite3"
    store = SQLiteStore(path)
    namespace_a = ("enterprise", "v1", "tenant-a", "user-a", "filesystem")
    namespace_b = ("enterprise", "v1", "tenant-a", "user-b", "filesystem")
    store.put(namespace_a, "/profile.md", {"content": "A", "kind": "profile"}, index=False)
    store.put(namespace_b, "/profile.md", {"content": "B", "kind": "profile"}, index=False)

    restarted = SQLiteStore(path)
    first_item = restarted.get(namespace_a, "/profile.md")
    assert first_item is not None
    assert first_item.value["content"] == "A"
    assert restarted.search(namespace_a, filter={"kind": "profile"})[0].value["content"] == "A"
    second_item = restarted.get(namespace_b, "/profile.md")
    assert second_item is not None
    assert second_item.value["content"] == "B"
    assert restarted.list_namespaces(prefix=("enterprise", "v1", "tenant-a")) == [namespace_a, namespace_b]


def test_sqlite_store_uses_wal_and_busy_timeout(tmp_path: Any) -> None:
    store = SQLiteStore(tmp_path / "enterprise-store.sqlite3", busy_timeout_ms=23_456)

    with closing(store._connect()) as connection:
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 23_456
        assert str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"


def test_sqlalchemy_store_persists_and_isolates_namespaces(tmp_path: Any) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'enterprise-store-sa.sqlite3'}"
    store = SQLAlchemyStore(url)
    namespace_a = ("enterprise", "v1", "tenant-a", "user-a", "filesystem")
    namespace_b = ("enterprise", "v1", "tenant-a", "user-b", "filesystem")
    store.put(namespace_a, "/profile.md", {"content": "A", "kind": "profile"}, index=False)
    store.put(namespace_b, "/profile.md", {"content": "B", "kind": "profile"}, index=False)

    restarted = SQLAlchemyStore(url)
    first_item = restarted.get(namespace_a, "/profile.md")
    assert first_item is not None
    assert first_item.value["content"] == "A"
    assert restarted.search(namespace_a, filter={"kind": "profile"})[0].value["content"] == "A"
    second_item = restarted.get(namespace_b, "/profile.md")
    assert second_item is not None
    assert second_item.value["content"] == "B"
    assert restarted.list_namespaces(prefix=("enterprise", "v1", "tenant-a")) == [namespace_a, namespace_b]


def test_langgraph_store_factory_prefers_sqlalchemy_url(tmp_path: Any) -> None:
    store = build_langgraph_store(
        sqlalchemy_url=f"sqlite+pysqlite:///{tmp_path / 'factory-store.sqlite3'}",
        sqlite_path=tmp_path / "fallback.sqlite3",
    )

    assert isinstance(store, SQLAlchemyStore)


def test_employee_memory_tools_use_only_the_authenticated_user_namespace(tmp_path: Any) -> None:
    store = SQLiteStore(tmp_path / "enterprise-store.sqlite3")
    settings = EnterpriseRuntimeSettings(tenant_id="wkzq", namespace_secret="test-secret")
    first_context = enterprise_runtime_context(_employee_context().to_dict(), "wecom:chenkang2", settings=settings)
    second_context = enterprise_runtime_context(
        dict(_employee_context().to_dict(), oa_account="other-user"),
        "wecom:other-user",
        settings=settings,
    )
    assert first_context is not None
    assert second_context is not None

    tools: dict[str, Any] = {tool.name: tool for tool in employee_memory_tools()}
    first_runtime = _tool_runtime(store, first_context)
    second_runtime = _tool_runtime(store, second_context)
    remembered = tools["remember_employee_memory"].func(
        memory="Prefer concise WeCom replies.",
        category="preference",
        runtime=first_runtime,
    )

    assert "recorded" in remembered
    recalled = tools["recall_employee_memory"].func(runtime=first_runtime)
    assert "[DurableEmployeeMemory]" in recalled
    assert "concise" in recalled
    assert "do not mix unrelated short-term" in recalled
    assert "No durable" in tools["recall_employee_memory"].func(runtime=second_runtime)
    refused = tools["remember_employee_memory"].func(
        memory="api key is private", category="preference", runtime=first_runtime
    )
    assert "Refused" in refused


def _tool_runtime(store: SQLiteStore, context: Mapping[str, object]) -> Any:
    from langgraph.prebuilt import ToolRuntime

    return ToolRuntime(
        state={},
        context=context,
        config={},
        stream_writer=lambda _chunk: None,
        tool_call_id=None,
        store=store,
    )
