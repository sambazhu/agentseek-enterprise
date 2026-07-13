import asyncio
import sys
from types import ModuleType

from agentseek_work.plugin import WorkPlugin


class FakeBinding:
    def __init__(self) -> None:
        self.calls = 0

    def load_message_state(self, message, session_id: str):
        del message, session_id
        return {"_work_message_key": "hmac-opaque"}

    def enrich_state(self, message, session_id: str, state) -> None:
        del message, session_id
        self.calls += 1
        state["digital_employee_profile"] = {"digital_employee_id": "industry-report"}


def test_plugin_is_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("AGENTSEEK_WORK_ENABLED", raising=False)
    state = {}

    asyncio.run(WorkPlugin().build_prompt({"content": "hello"}, "session", state))

    assert state == {}


def test_plugin_loads_template_binding_and_enriches_once(monkeypatch) -> None:
    binding = FakeBinding()
    module = ModuleType("fake_work_binding")
    module.build = lambda: binding  # ty: ignore[unresolved-attribute]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setenv("AGENTSEEK_WORK_ENABLED", "true")
    monkeypatch.setenv("AGENTSEEK_WORK_BINDING", "fake_work_binding:build")
    plugin = WorkPlugin()
    state = asyncio.run(plugin.load_state({"content": "first"}, "session"))

    asyncio.run(plugin.build_prompt({"content": "first"}, "session", state))
    asyncio.run(plugin.build_prompt({"content": "second"}, "session", state))

    assert binding.calls == 1
    assert state["_work_message_key"] == "hmac-opaque"
    assert state["digital_employee_profile"] == {"digital_employee_id": "industry-report"}
    assert state["_work_enriched"] is True


def test_plugin_fails_closed_without_exposing_exception_text(monkeypatch) -> None:
    class BrokenBinding:
        def load_message_state(self, message, session_id: str):
            del message, session_id
            raise RuntimeError

        def enrich_state(self, message, session_id: str, state) -> None:
            del message, session_id, state
            raise RuntimeError

    module = ModuleType("broken_work_binding")
    module.build = BrokenBinding  # ty: ignore[unresolved-attribute]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setenv("AGENTSEEK_WORK_ENABLED", "true")
    monkeypatch.setenv("AGENTSEEK_WORK_BINDING", "broken_work_binding:build")
    state = asyncio.run(WorkPlugin().load_state({"content": "hello"}, "session"))

    asyncio.run(WorkPlugin().build_prompt({"content": "hello"}, "session", state))

    assert state["digital_employee_status"] == "not_configured"
    assert state["_work_binding_error"] == "RuntimeError"
    assert "secret" not in str(state)
