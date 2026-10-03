"""Bounded identity waits through the actual channel/plugin chain; no real send."""

# ruff: noqa: F811
import asyncio
from threading import Event
from types import SimpleNamespace

import pytest
from agentseek_wecom.file_delivery import file_ref
from enterprise_wecom_digital_employee.native_file_delivery import native_file_tools
from test_sandbox_multiuser_integration import pair, rig  # noqa: F401


@pytest.mark.parametrize("layer", ["userid", "employee"])
def test_timeout_denies_delivery_and_late_result_does_not_restore_authority(pair, monkeypatch, layer):
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_IDENTITY_CACHE_ENABLED", "false")
    monkeypatch.setenv("AGENTSEEK_ENTERPRISE_IDENTITY_LOOKUP_TIMEOUT_SECONDS", "0.05")
    channel = pair.rig.channel
    monkeypatch.setattr(channel.settings, "api_timeout_seconds", 0.05)
    enterprise = pair.plugin()
    resolver, provider = channel._userid_resolver, enterprise._provider
    entered, release, finished = Event(), Event(), Event()

    def blocking_lookup(value, original):
        if value in {"alice", "open-alice"}:
            entered.set()
            try:
                assert release.wait(timeout=5)
                return original(value)
            finally:
                finished.set()
        return original(value)

    async def scenario():
        initial = await pair.receive([pair.message("alice", "aibot_callback")], enterprise=enterprise)
        assert native_file_tools()[0].func(initial["wecom:alice"])["files"]
        if layer == "userid":
            channel._userid_resolver = SimpleNamespace(
                resolve=lambda value: blocking_lookup(value, resolver.resolve))
        else:
            enterprise._provider = SimpleNamespace(
                get_employee_context=lambda value: blocking_lookup(value, provider.get_employee_context))
        try:
            seen = await asyncio.wait_for(pair.receive([
                pair.message("alice", "aibot_callback", message_id="timeout-alice"),
                pair.message("bob", "aibot_callback", message_id="healthy-bob"),
            ], enterprise=enterprise), timeout=2)
            assert entered.is_set() and not finished.is_set()
            healthy = seen.pop("wecom:bob")
            assert native_file_tools()[0].func(healthy)["files"][0]["file_ref"] == file_ref(pair.records["bob"])
            denied = next(iter(seen.values()))
            assert not denied.context
            assert native_file_tools()[0].func(denied) == {"status": "unavailable", "files": []}
            outcome = await native_file_tools()[1].coroutine(file_ref(pair.records["alice"]), denied)
            assert outcome["status"] == "denied" and pair.calls == []
            # Timeout releases the waiter, not the underlying synchronous thread.
            release.set()
            assert await asyncio.to_thread(finished.wait, 2)
            assert not denied.context
            assert native_file_tools()[0].func(denied)["status"] == "unavailable"
            assert enterprise._identity_cache == {}
            channel._userid_resolver, enterprise._provider = resolver, provider
            recovered = await pair.receive([
                pair.message("alice", "aibot_callback", message_id="fresh-alice"),
            ], enterprise=enterprise)
            assert native_file_tools()[0].func(recovered["wecom:alice"])["files"][0]["file_ref"] == file_ref(pair.records["alice"])
            assert pair.calls == []
        finally:
            release.set()
            channel._userid_resolver, enterprise._provider = resolver, provider

    asyncio.run(scenario())
