from __future__ import annotations

import asyncio
from types import SimpleNamespace

import agentseek_langchain.plugin as plugin_module
from agentseek_langchain.profiles import text_spec
from agentseek_langchain.shapes import ObjectDict, copy_str_mapping
from agentseek_langchain.spec import RunnableSpec
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, LLMResult


class _AsyncRunnable:
    def __init__(self, output: str) -> None:
        self.output = output
        self.calls: list[tuple[object, ObjectDict | None]] = []

    async def ainvoke(self, runnable_input: object, config: ObjectDict | None = None) -> str:
        self.calls.append((runnable_input, config))
        return self.output


class _AsyncRunnableWithContext:
    def __init__(self, output: str) -> None:
        self.output = output
        self.calls: list[tuple[object, ObjectDict | None, ObjectDict | None]] = []

    async def ainvoke(
        self,
        runnable_input: object,
        config: ObjectDict | None = None,
        context: ObjectDict | None = None,
    ) -> str:
        self.calls.append((runnable_input, config, context))
        return self.output


class _HangingRunnable:
    async def ainvoke(self, runnable_input: object, config: ObjectDict | None = None) -> str:
        del runnable_input, config
        await asyncio.Event().wait()
        return "unreachable"


class _HookRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object, str, ObjectDict]] = []

    async def call_many(self, hook_name: str, *, message: object, session_id: str, state: ObjectDict) -> list[object]:
        self.calls.append((hook_name, message, session_id, state))
        state["_contextseek_block"] = "[RetrievedEmployeeSemanticMemory]\n- remembered preference"
        return []


def test_plugin_run_model_delegates_to_loaded_spec(monkeypatch, tmp_path) -> None:
    runnable = _AsyncRunnable("delegated-output")
    spec = text_spec(runnable)
    info_messages: list[str] = []

    monkeypatch.setattr(plugin_module, "get_langchain_settings", lambda: SimpleNamespace(SPEC="dummy:SPEC"))
    monkeypatch.setattr(plugin_module, "load_spec_from_path", lambda path: spec)
    monkeypatch.setattr(plugin_module.logger, "info", info_messages.append)

    plugin = plugin_module.LangChainRunnablePlugin()
    result = asyncio.run(
        plugin.run_model(
            prompt="hello",
            session_id="session-1",
            state={"_runtime_workspace": str(tmp_path)},
        )
    )

    assert result == "delegated-output"
    assert runnable.calls[0][0] == "hello"
    metadata = copy_str_mapping(runnable.calls[0][1].get("metadata") if runnable.calls[0][1] else None)
    assert metadata == {
        "session_id": "session-1",
        "workspace": str(tmp_path),
    }
    callbacks = runnable.calls[0][1].get("callbacks") if runnable.calls[0][1] else None
    assert isinstance(callbacks, list)
    assert isinstance(callbacks[0], plugin_module._ModelCallObservability)
    assert info_messages == ["Using LangChain spec entrypoint: dummy:SPEC"]


def test_model_call_observability_emits_sizes_latency_and_usage_without_content(monkeypatch) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        plugin_module,
        "_emit_enterprise_event",
        lambda event, **fields: events.append((event, fields)),
    )
    handler = plugin_module._ModelCallObservability("wecom:employee")

    async def scenario() -> None:
        await handler.on_chat_model_start(
            {"name": "ChatOpenAI"},
            [[SystemMessage(content="system prompt"), HumanMessage(content="private request")]],
            run_id=plugin_module.UUID(int=1),
            invocation_params={"model": "qwen-flash", "tools": [{"name": "tool-a"}]},
        )
        await handler.on_llm_end(
            LLMResult(generations=[[ChatGeneration(message=AIMessage(
                content="private answer",
                usage_metadata={"input_tokens": 11, "output_tokens": 3, "total_tokens": 14},
            ))]]),
            run_id=plugin_module.UUID(int=1),
        )

    asyncio.run(scenario())

    assert events[0][0] == "langchain_model_call"
    fields = events[0][1]
    assert fields["status"] == "succeeded"
    assert fields["session_id"] == "wecom:employee"
    assert fields["input_chars"] == len("system promptprivate request")
    assert fields["system_chars"] == len("system prompt")
    assert fields["message_count"] == 2
    assert fields["tool_count"] == 1
    assert fields["model_name"] == "qwen-flash"
    assert fields["output_chars"] == len("private answer")
    assert fields["prompt_tokens"] == 11
    assert fields["completion_tokens"] == 3
    assert fields["total_tokens"] == 14
    assert "private request" not in repr(fields)
    assert "private answer" not in repr(fields)


def test_plugin_enriches_state_before_spec_build_input(monkeypatch, tmp_path) -> None:
    runnable = _AsyncRunnable("delegated-output")

    def build_input(context):
        return {
            "prompt": context.prompt,
            "semantic_memory": context.state.get("_contextseek_block"),
        }

    spec = RunnableSpec(
        runnable=runnable,
        build_input=build_input,
        parse_output=str,
    )
    hook_runtime = _HookRuntime()
    framework = SimpleNamespace(_hook_runtime=hook_runtime)

    monkeypatch.setattr(plugin_module, "get_langchain_settings", lambda: SimpleNamespace(SPEC="dummy:SPEC"))
    monkeypatch.setattr(plugin_module, "load_spec_from_path", lambda path: spec)

    plugin = plugin_module.LangChainRunnablePlugin(framework)
    result = asyncio.run(
        plugin.run_model(
            prompt="hello",
            session_id="session-1",
            state={"_runtime_workspace": str(tmp_path)},
        )
    )

    assert result == "delegated-output"
    assert hook_runtime.calls[0][0] == "build_prompt"
    assert hook_runtime.calls[0][1] == {"content": "hello", "session_id": "session-1"}
    assert runnable.calls[0][0] == {
        "prompt": "hello",
        "semantic_memory": "[RetrievedEmployeeSemanticMemory]\n- remembered preference",
    }
    assert hook_runtime.calls[0][3]["_contextseek_block"] == "[RetrievedEmployeeSemanticMemory]\n- remembered preference"


def test_plugin_run_model_stream_wraps_single_result(monkeypatch, tmp_path) -> None:
    runnable = _AsyncRunnable("streamed-once")
    spec = text_spec(runnable)

    monkeypatch.setattr(plugin_module, "get_langchain_settings", lambda: SimpleNamespace(SPEC="dummy:SPEC"))
    monkeypatch.setattr(plugin_module, "load_spec_from_path", lambda path: spec)

    plugin = plugin_module.LangChainRunnablePlugin()
    stream = asyncio.run(
        plugin.run_model_stream(
            prompt="hello",
            session_id="session-1",
            state={"_runtime_workspace": str(tmp_path)},
        )
    )
    events = asyncio.run(_collect_events(stream))

    assert [(event.kind, event.data) for event in events] == [
        ("text", {"delta": "streamed-once"}),
        ("final", {"text": "streamed-once", "ok": True}),
    ]


def test_plugin_run_model_times_out_without_hanging_session(monkeypatch, tmp_path) -> None:
    spec = text_spec(_HangingRunnable())
    monkeypatch.setattr(
        plugin_module,
        "get_langchain_settings",
        lambda: SimpleNamespace(SPEC="dummy:SPEC", RUN_TIMEOUT_SECONDS=0.01),
    )
    monkeypatch.setattr(plugin_module, "load_spec_from_path", lambda path: spec)

    result = asyncio.run(
        plugin_module.LangChainRunnablePlugin().run_model(
            prompt="hello",
            session_id="session-timeout",
            state={"_runtime_workspace": str(tmp_path)},
        )
    )

    assert result == plugin_module._MODEL_TIMEOUT_MESSAGE


def test_plugin_run_model_stream_finishes_after_timeout(monkeypatch, tmp_path) -> None:
    spec = text_spec(_HangingRunnable())
    monkeypatch.setattr(
        plugin_module,
        "get_langchain_settings",
        lambda: SimpleNamespace(SPEC="dummy:SPEC", RUN_TIMEOUT_SECONDS=0.01),
    )
    monkeypatch.setattr(plugin_module, "load_spec_from_path", lambda path: spec)
    plugin = plugin_module.LangChainRunnablePlugin()

    stream = asyncio.run(
        plugin.run_model_stream(
            prompt="hello",
            session_id="session-stream-timeout",
            state={"_runtime_workspace": str(tmp_path)},
        )
    )
    events = asyncio.run(_collect_events(stream))

    assert [(event.kind, event.data) for event in events] == [
        ("text", {"delta": plugin_module._MODEL_TIMEOUT_MESSAGE}),
        ("final", {"text": plugin_module._MODEL_TIMEOUT_MESSAGE, "ok": False}),
    ]


def test_plugin_passes_ag_ui_context_as_runtime_context(monkeypatch, tmp_path) -> None:
    runnable = _AsyncRunnableWithContext("delegated-output")
    spec = text_spec(runnable)

    monkeypatch.setattr(plugin_module, "get_langchain_settings", lambda: SimpleNamespace(SPEC="dummy:SPEC"))
    monkeypatch.setattr(plugin_module, "load_spec_from_path", lambda path: spec)

    plugin = plugin_module.LangChainRunnablePlugin()
    result = asyncio.run(
        plugin.run_model(
            prompt="hello",
            session_id="session-1",
            state={
                "_runtime_workspace": str(tmp_path),
                "_ag_ui": {
                    "context": [
                        {"description": "tenant", "value": "demo"},
                        {
                            "description": "output_schema",
                            "value": '{"type":"object","properties":{"name":{"type":"string"}}}',
                        },
                    ]
                },
            },
        )
    )

    assert result == "delegated-output"
    assert runnable.calls[0][2] == {
        "tenant": "demo",
        "output_schema": {"type": "object", "properties": {"name": {"type": "string"}}},
    }


def test_plugin_merges_enterprise_and_ag_ui_runtime_context(monkeypatch, tmp_path) -> None:
    runnable = _AsyncRunnableWithContext("delegated-output")
    spec = text_spec(runnable)

    monkeypatch.setattr(plugin_module, "get_langchain_settings", lambda: SimpleNamespace(SPEC="dummy:SPEC"))
    monkeypatch.setattr(plugin_module, "load_spec_from_path", lambda path: spec)

    plugin = plugin_module.LangChainRunnablePlugin()
    result = asyncio.run(
        plugin.run_model(
            prompt="hello",
            session_id="session-1",
            state={
                "_runtime_workspace": str(tmp_path),
                "_langgraph_runtime_context": {
                    "enterprise": {"tenant_key": "hmac-tenant", "user_key": "hmac-user"}
                },
                "_ag_ui": {"context": [{"description": "tenant", "value": "demo"}]},
            },
        )
    )

    assert result == "delegated-output"
    assert runnable.calls[0][2] == {
        "enterprise": {"tenant_key": "hmac-tenant", "user_key": "hmac-user"},
        "tenant": "demo",
    }


def test_plugin_falls_back_to_default_model_entrypoint_without_spec(monkeypatch, tmp_path) -> None:
    warning_messages: list[str] = []
    load_calls: list[str] = []

    monkeypatch.setattr(plugin_module, "get_langchain_settings", lambda: SimpleNamespace(SPEC="   "))
    monkeypatch.setattr(plugin_module, "load_spec_from_path", load_calls.append)
    monkeypatch.setattr(plugin_module.logger, "warning", warning_messages.append)

    plugin = plugin_module.LangChainRunnablePlugin()

    result = asyncio.run(
        plugin.run_model(
            prompt="hello",
            session_id="session-1",
            state={"_runtime_workspace": str(tmp_path)},
        )
    )
    stream = asyncio.run(
        plugin.run_model_stream(
            prompt="hello",
            session_id="session-1",
            state={"_runtime_workspace": str(tmp_path)},
        )
    )

    assert result is None
    assert stream is None
    assert load_calls == []
    assert warning_messages == ["LangChain spec not configured; falling back to the default model entrypoint."]


async def _collect_events(stream) -> list:
    return [event async for event in stream]
