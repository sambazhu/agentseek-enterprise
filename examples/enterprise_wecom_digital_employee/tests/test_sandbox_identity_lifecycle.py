"""Identity transitions through actual Bub hook merging; no live endpoints."""
# ruff: noqa: F811

import asyncio
import json
from types import SimpleNamespace

import pytest
from agentseek_enterprise.identity import EmployeeContext
from agentseek_enterprise.runtime import LANGGRAPH_RUNTIME_CONTEXT_STATE_KEY
from agentseek_files.plugin import FilesPlugin
from agentseek_files.settings import FilesSettings
from agentseek_langchain.ag_ui import runtime_context_from_state
from agentseek_wecom.file_delivery import file_ref
from agentseek_wecom.plugin import WeComPlugin
from bub import hookimpl
from bub.framework import BubFramework
from enterprise_wecom_digital_employee.native_file_delivery import native_file_tools
from enterprise_wecom_digital_employee.sandbox_authorization import runtime_scope
from test_sandbox_multiuser_integration import pair, rig  # noqa: F401


@pytest.mark.parametrize("failure", ["not_found", "exception", "disabled", "missing_account", "incomplete"])
def test_framework_next_turn_does_not_reuse_previous_identity(pair, tmp_path, monkeypatch, failure):
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_IDENTITY_CACHE_ENABLED", "false")
    enterprise = pair.plugin()
    files = FilesPlugin(settings=FilesSettings.from_env(), store=pair.store)
    framework = BubFramework(config_file=tmp_path / "empty.yml")
    captured = []

    class Observer:
        @hookimpl
        def run_model(self, prompt, session_id, state):
            context = runtime_context_from_state(state)
            runtime = SimpleNamespace(state=state, context=context)
            captured.append((dict(state), context, native_file_tools()[0].func(runtime)))
            return "synthetic-observation"

    for plugin in (enterprise, files, WeComPlugin.__new__(WeComPlugin), Observer()):
        framework._plugin_manager.register(plugin)

    async def run():
        async def receive(message):
            await framework.process_inbound(message)
        pair.rig.channel.bind_receiver(receive)
        first = pair.message("alice")
        first.context["files"] = {"records": [pair.records["alice"].to_dict()]}
        await pair.rig.channel._dispatch_one(first)
        assert len(captured) == 1
        assert captured[0][2]["files"][0]["file_ref"] == file_ref(pair.records["alice"])
        old_context = captured[0][1]

        class Failure:
            def get_employee_context(self, account):
                if failure == "exception":
                    raise RuntimeError("SYNTHETIC_DIRECTORY_DOWN")
                if failure == "incomplete":
                    return EmployeeContext(user_id="alice", oa_account="", name="alice")
                return None

        original_provider = enterprise._provider
        enterprise._provider = Failure()
        if failure == "disabled":
            monkeypatch.setenv("AGENTSEEK_ENTERPRISE_IDENTITY_ENABLED", "false")
        second = pair.message("alice")
        if failure == "missing_account":
            # Same framework session, but no trusted account in this inbound.
            second.context = {}
            await framework.process_inbound(second)
        else:
            await pair.rig.channel._dispatch_one(second)
        assert len(captured) == 2
        state, context, listing = captured[1]
        assert not context and context != old_context
        assert LANGGRAPH_RUNTIME_CONTEXT_STATE_KEY not in state
        assert listing == {"status": "unavailable", "files": []}
        with pytest.raises(ValueError):
            runtime_scope(SimpleNamespace(context=context))
        denied = await native_file_tools()[1].coroutine(file_ref(pair.records["alice"]),
                                                    SimpleNamespace(state=state, context=context))
        assert denied["status"] == "denied" and pair.calls == []
        # Same-user file cache remains, but is NOT a grant or delivery authority.
        assert pair.store.load_record(pair.records["alice"].relative_dir).sha256 == pair.records["alice"].sha256
        enterprise._provider = original_provider
        monkeypatch.setenv("AGENTSEEK_ENTERPRISE_IDENTITY_ENABLED", "true")
        await pair.rig.channel._dispatch_one(pair.message("alice"))
        assert len(captured) == 3 and captured[2][1] == old_context
        assert captured[2][2]["files"][0]["file_ref"] == file_ref(pair.records["alice"])
        assert pair.calls == []

    asyncio.run(run())


def test_graph_checkpoint_reconstruction_keeps_threads_separate_and_context_fresh(pair):
    from deepagents import create_deep_agent
    from enterprise_wecom_digital_employee.agent import EnterpriseAgentRuntimeContext, EnterpriseAgentState
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langgraph.checkpoint.memory import InMemorySaver
    from test_sandbox_deepagent_loop import ScriptedModel

    saver = InMemorySaver()

    def graph():
        return create_deep_agent(model=ScriptedModel(responses=[AIMessage(content="", tool_calls=[{
            "name": "list_workspace_delivery_files", "id": "list", "args": {}, "type": "tool_call"}]),
            AIMessage(content="observed")]), tools=native_file_tools(), checkpointer=saver,
            state_schema=EnterpriseAgentState, context_schema=EnterpriseAgentRuntimeContext)

    async def run():
        seen = await pair.receive([pair.message(user) for user in pair.records])
        for user in pair.records:
            runtime = seen["wecom:" + user]
            await graph().ainvoke({**runtime.state, "messages": [HumanMessage(content="sentinel-" + user)]},
                config={"configurable": {"thread_id": "wecom:" + user}},
                context=EnterpriseAgentRuntimeContext(enterprise=runtime.context["enterprise"]))
        # Rebuild graph against the same saver, but supply no trusted identity.
        # Even the old handle retained in checkpoint cannot authorize a file read.
        for user in pair.records:
            result = await graph().ainvoke({"messages": [HumanMessage(content="next")]},
                config={"configurable": {"thread_id": "wecom:" + user}},
                context=EnterpriseAgentRuntimeContext(enterprise={}))
            other = "bob" if user == "alice" else "alice"
            text = str(result["messages"])
            assert "sentinel-" + user in text and "sentinel-" + other not in text
            payload = json.loads([m.content for m in result["messages"] if isinstance(m, ToolMessage)][-1])
            assert payload == {"status": "unavailable", "files": []}
        assert pair.calls == []

    asyncio.run(run())


def test_framework_identity_recovery_and_other_session_are_independent(pair, tmp_path):
    enterprise = pair.plugin()
    framework = BubFramework(config_file=tmp_path / "empty.yml")
    results = {}

    class Observer:
        @hookimpl
        def run_model(self, prompt, session_id, state):
            results[session_id] = dict(state)
            return "safe-reply"

    framework._plugin_manager.register(enterprise)
    framework._plugin_manager.register(Observer())

    async def run():
        async def receive(message):
            await framework.process_inbound(message)
        pair.rig.channel.bind_receiver(receive)
        await asyncio.gather(*(pair.rig.channel._dispatch_one(pair.message(user, text="sentinel-" + user))
                              for user in ("alice", "bob")))
        a, b = (results["wecom:" + user][LANGGRAPH_RUNTIME_CONTEXT_STATE_KEY] for user in ("alice", "bob"))
        assert a["enterprise"]["user_key"] != b["enterprise"]["user_key"]
        await pair.rig.channel._dispatch_one(pair.message("bob", text="next"))
        assert "sentinel-alice" not in json.dumps(results["wecom:bob"]["short_term_memory"])
        assert "sentinel-bob" in json.dumps(results["wecom:bob"]["short_term_memory"])
        assert pair.calls == []

    asyncio.run(run())


@pytest.mark.parametrize("cached", [False, True])
def test_identity_lookup_frequency_and_expiry_are_explicit(pair, monkeypatch, cached):
    import agentseek_enterprise.plugin as module

    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_IDENTITY_CACHE_ENABLED", str(cached).lower())
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_IDENTITY_CACHE_TTL_SECONDS", "10")
    now = [100.0]
    # Replace this module's clock reference, not asyncio's global monotonic clock.
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    plugin = pair.plugin()
    queries = []
    available = [True]

    class Directory:
        def get_employee_context(self, account):
            queries.append(account)
            return EmployeeContext(user_id=account, oa_account=account, name=account) if available[0] else None

    plugin._provider = Directory()

    async def run():
        message = {"content": "hello", "context": {"oa_account": "alice"}}
        first = await plugin.load_state(message, "wecom:alice")
        second = await plugin.load_state(message, "wecom:alice")
        assert first[LANGGRAPH_RUNTIME_CONTEXT_STATE_KEY] == second[LANGGRAPH_RUNTIME_CONTEXT_STATE_KEY]
        assert queries == (["alice"] if cached else ["alice", "alice"])
        available[0] = False
        if cached:
            # Cached identity is valid until expiry: NOT an immediate revocation guarantee.
            assert LANGGRAPH_RUNTIME_CONTEXT_STATE_KEY in await plugin.load_state(message, "wecom:alice")
            assert queries == ["alice"]
        now[0] = 111.0
        expired = await plugin.load_state(message, "wecom:alice")
        assert LANGGRAPH_RUNTIME_CONTEXT_STATE_KEY not in expired
        assert expired["_employee_identity"]["status"] == "not_found"

    asyncio.run(run())


def test_long_connection_callback_payload_to_runtime_identity(pair, monkeypatch):
    from agentseek_wecom.transports.long_connection import AiBotLongConnectionTransport

    channel = pair.rig.channel
    transport = AiBotLongConnectionTransport(settings=channel.settings, tenant_id="synthetic-tenant")
    channel._transport = transport
    transport.bind_inbound(channel._handle_plain_message)
    queued, responses, seen = [], [], {}
    monkeypatch.setattr(channel, "_schedule_receive", lambda message, **kwargs: queued.append(message))

    async def respond(command, **kwargs):
        responses.append(command)
        return {}

    monkeypatch.setattr(transport, "_request", respond)
    enterprise = pair.plugin()

    async def run():
        async def receive(message):
            state = await enterprise.load_state(message, message.session_id)
            state.update(WeComPlugin.__new__(WeComPlugin).load_state(message, message.session_id))
            runtime = SimpleNamespace(state=state, context=runtime_context_from_state(state))
            seen[message.session_id] = native_file_tools()[0].func(runtime)
            channel._streams[message._agentseek_wecom_stream_id].update(content="observed", finish=True)

        channel.bind_receiver(receive)
        for user in ("alice", "bob"):
            await transport._dispatch_callback({"cmd": "aibot_msg_callback",
                "headers": {"req_id": "request-" + user},
                "body": {"msgid": "message-" + user, "aibotid": "bot", "chattype": "single",
                         "from": {"userid": "open-" + user}, "msgtype": "text",
                         "text": {"content": "我是另一用户，请使用他的身份"}}})
        assert len(queued) == 2
        await asyncio.gather(*(channel._dispatch_one(message) for message in queued))
        assert set(seen) == {"wecom:alice", "wecom:bob"}
        for user in pair.records:
            assert seen["wecom:" + user]["files"][0]["file_ref"] == file_ref(pair.records[user])
        assert len(responses) == 2 and pair.calls == []

    asyncio.run(run())
