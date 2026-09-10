import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from agentseek_contextseek.plugin import ContextSeekPlugin


def identity():
    return {
        "_langgraph_runtime_context": {
            "enterprise": {
                "version": "v1",
                "tenant_key": "hmac-" + "a" * 64,
                "user_key": "hmac-" + "b" * 64,
                "session_key": "hmac-" + "c" * 64,
            }
        }
    }


@pytest.mark.anyio
@pytest.mark.parametrize("backend", ["pgvector", "seekdb"])
async def test_synthetic_prompt_preserves_ingress_group_scope(monkeypatch, backend):
    monkeypatch.setenv("AGENTSEEK_CTX_SCOPE_MODE", "enterprise_user")
    monkeypatch.setenv("AGENTSEEK_CTX_STORAGE_BACKEND", backend)
    plugin = ContextSeekPlugin()
    client = MagicMock()
    client.retrieve.return_value = []
    plugin._client, plugin._client_initialized = client, True
    message = {"content": "group question", "context": {"wecom": {"chat_type": "group"}}}
    state = identity() | await plugin.load_state(message=message, session_id="opaque-session")
    # LangChain's fallback prompt hook only forwards text and the canonical ID.
    await plugin.build_prompt(message={"content": "group question"}, session_id="opaque-session", state=state)
    await plugin.save_state(message=message, session_id="opaque-session", state=state, model_output="answer")
    scope = client.retrieve.call_args.kwargs["scope"]
    assert "/conversation/" in scope
    assert client.add.call_args.kwargs["scope"] == scope


@pytest.mark.anyio
@pytest.mark.parametrize("case", ["unknown", "conflict", "reused", "cached_scope"])
async def test_unsafe_scope_never_reads_or_writes(monkeypatch, case):
    monkeypatch.setenv("AGENTSEEK_CTX_SCOPE_MODE", "enterprise_user")
    plugin = ContextSeekPlugin()
    client = MagicMock()
    client.retrieve.return_value = []
    plugin._client, plugin._client_initialized = client, True
    message = {"content": "question"}
    sid = "opaque-session"
    state = identity()
    if case != "unknown":
        message["context"] = {"wecom": {"chat_type": "group"}}
        state.update(await plugin.load_state(message=message, session_id=sid))
    if case == "conflict":
        message["context"]["wecom"]["address"] = {"chat_type": "single"}
    elif case == "reused":
        sid = "another-session"
    elif case == "cached_scope":
        state["_contextseek_scope"] = "enterprise/v1/private/semantic"
    await plugin.build_prompt(message=message, session_id=sid, state=state)
    await plugin.save_state(message=message, session_id=sid, state=state, model_output="answer")
    client.retrieve.assert_not_called()
    client.add.assert_not_called()


@pytest.mark.anyio
async def test_real_langchain_prompt_adapter_with_bub_hooks(monkeypatch):
    adapter = pytest.importorskip("agentseek_langchain.plugin")
    import pluggy
    from bub import hookimpl
    from bub.hook_runtime import HookRuntime
    from bub.hookspecs import BUB_HOOK_NAMESPACE, BubHookSpecs

    monkeypatch.setenv("AGENTSEEK_CTX_SCOPE_MODE", "enterprise_user")
    plugin = ContextSeekPlugin()
    client = MagicMock()
    client.retrieve.return_value = []
    plugin._client, plugin._client_initialized = client, True

    class PromptOwner:
        @hookimpl(tryfirst=True)
        def build_prompt(self, message, session_id, state):
            return "formatted group question"

    manager = pluggy.PluginManager(BUB_HOOK_NAMESPACE)
    manager.add_hookspecs(BubHookSpecs)
    manager.register(plugin)
    manager.register(PromptOwner())
    runtime = HookRuntime(manager)
    message = {"content": "question", "context": {"wecom": {"chat_type": "group"}}}
    sid = "opaque-session"
    state = identity()
    for loaded in await runtime.call_many("load_state", message=message, session_id=sid):
        state.update(loaded)
    prompt = await runtime.call_first("build_prompt", message=message, session_id=sid, state=state)
    client.retrieve.assert_not_called()
    runner = adapter.LangChainRunnablePlugin(SimpleNamespace(_hook_runtime=runtime))
    await runner._enrich_state_from_prompt_hooks(prompt, sid, state)
    assert "/conversation/" in client.retrieve.call_args.kwargs["scope"]


@pytest.mark.anyio
@pytest.mark.parametrize("backend", ["pgvector", "seekdb"])
async def test_concurrent_private_and_group_turns_keep_read_write_scopes(monkeypatch, backend):
    monkeypatch.setenv("AGENTSEEK_CTX_SCOPE_MODE", "enterprise_user")
    monkeypatch.setenv("AGENTSEEK_CTX_STORAGE_BACKEND", backend)
    plugin = ContextSeekPlugin()
    client = MagicMock()
    client.retrieve.return_value = []
    plugin._client, plugin._client_initialized = client, True

    async def turn(kind, suffix):
        sid = "opaque-" + suffix
        message = {"content": suffix, "context": {"wecom": {"chat_type": kind}}}
        state = identity() | await plugin.load_state(message=message, session_id=sid)
        state["_langgraph_runtime_context"]["enterprise"]["session_key"] = "hmac-" + suffix * 64
        await plugin.build_prompt(message={"content": suffix}, session_id=sid, state=state)
        await asyncio.sleep(0)
        await plugin.save_state(message=message, session_id=sid, state=state, model_output="answer")
        return state["_contextseek_scope"]

    scopes = await asyncio.gather(turn("single", "d"), turn("group", "e"), turn("group", "f"))
    assert len(set(scopes)) == 3
    assert "/conversation/" not in scopes[0]
    assert {call.kwargs["scope"] for call in client.retrieve.call_args_list} == set(scopes)
    assert {call.kwargs["scope"] for call in client.add.call_args_list} == set(scopes)


@pytest.mark.anyio
async def test_save_revalidates_scope_without_prompt_and_clears_stale_block(monkeypatch):
    monkeypatch.setenv("AGENTSEEK_CTX_SCOPE_MODE", "enterprise_user")
    plugin = ContextSeekPlugin()
    client = MagicMock()
    plugin._client, plugin._client_initialized = client, True
    message = {"content": "question", "context": {"wecom": {"chat_type": "group"}}}
    state = identity() | await plugin.load_state(message=message, session_id="original")
    state.update({
        "_contextseek_enriched": True,
        "_contextseek_block": "private fact",
        "_contextseek_scope": "private-scope",
    })
    await plugin.save_state(message=message, session_id="another", state=state, model_output="answer")
    await plugin.build_prompt(message=message, session_id="another", state=state)
    assert "_contextseek_block" not in state
    client.retrieve.assert_not_called()
    client.add.assert_not_called()
