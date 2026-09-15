import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from enterprise_wecom_digital_employee import agent


@pytest.fixture
def retry(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(agent, "_SKILL_MCP_SYNC_RESULT", None)
    monkeypatch.setattr(agent, "_SKILL_MCP_SYNC_ATTEMPTS", 0)
    monkeypatch.setattr(agent, "_SKILL_MCP_SYNC_NEXT", 0.0)
    monkeypatch.setattr(agent, "_SKILL_MCP_SYNC_LOCK", threading.Lock())
    monkeypatch.setattr(agent.time, "monotonic", lambda: clock[0])
    return clock


@pytest.mark.parametrize("succeeds", [True, False])
def test_cooldown_success_cache_and_attempt_cap(retry, monkeypatch, succeeds):
    calls = []
    async def sync():
        calls.append(1)
        return (object(), "prompt") if succeeds and len(calls) == 2 else (None, "")
    monkeypatch.setattr(agent, "_sync_platform_config", sync)
    assert agent._get_skill_mcp_sync_result() == (None, "")
    for _ in range(3):
        agent._get_skill_mcp_sync_result()
    assert len(calls) == 1
    for _ in range(5):
        retry[0] += 60
        agent._get_skill_mcp_sync_result()
    assert len(calls) == (2 if succeeds else 3)


def test_concurrent_build_does_not_wait_or_duplicate(retry, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    async def sync():
        entered.set()
        assert release.wait(5)
        return object(), "prompt"
    monkeypatch.setattr(agent, "_sync_platform_config", sync)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(agent._get_skill_mcp_sync_result)
        try:
            assert entered.wait(5)
            assert agent._get_skill_mcp_sync_result() == (None, "")
            assert agent._SKILL_MCP_SYNC_ATTEMPTS == 1
        finally:
            release.set()
        assert future.result()[1] == "prompt"
