import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from enterprise_wecom_digital_employee import agent, mcp_config_merge, tools


@pytest.mark.parametrize("failure", [False, True])
def test_sync_once_then_merge_cached_platform_and_local(tmp_path, monkeypatch, failure):
    import agentseek_skill_mcp as sdk

    directory = tmp_path / ".agents"
    directory.mkdir()
    cache, local, effective = (directory / name for name in ("mcp.dmcp.json", "mcp.local.json", "mcp.json"))
    cache.write_text('{"mcpServers":{"platform":{}}}')
    local.write_text('{"mcpServers":{"department-knowledge":{}}}')
    load = AsyncMock(side_effect=RuntimeError("private-secret")) if failure else AsyncMock(return_value=SimpleNamespace(skills=[], mcps=[]))
    sync = AsyncMock(return_value={"mcpServers": {}})
    monkeypatch.setattr(sdk, "load_agent_config", load)
    monkeypatch.setattr(sdk, "sync_mcp_config", sync)
    monkeypatch.setattr(sdk, "sync_skills", lambda _: [])
    monkeypatch.setattr(sdk, "build_skill_system_prompt", lambda *a: "")
    monkeypatch.setattr(agent, "_patch_sdk_skill_for_null_category", lambda: None)
    monkeypatch.setattr(agent, "load_static_agent_assets", lambda _: None)
    monkeypatch.setattr(agent, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(agent, "get_settings", lambda: SimpleNamespace(resolved_mcp_config_path=lambda: effective))
    monkeypatch.setattr(agent, "_SKILL_MCP_SYNC_RESULT", None)
    monkeypatch.delenv("AGENTSEEK_SKILL_MCP_MCP_JSON_PATH", raising=False)
    for _ in range(2):
        agent._get_skill_mcp_sync_result()
        assert mcp_config_merge.merge_mcp_config(agent._platform_mcp_path(), local, effective)
    assert load.await_count == 1
    assert sync.await_count == (0 if failure else 1)
    if not failure:
        assert sync.call_args.kwargs["mcp_json_path"] == str(cache)
    assert set(json.loads(effective.read_text())["mcpServers"]) == {"platform", "department-knowledge"}


def test_old_path_collision_skips_sync(tmp_path, monkeypatch):
    import agentseek_skill_mcp as sdk

    effective = tmp_path / ".agents/mcp.json"
    monkeypatch.setattr(agent, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("AGENTSEEK_SKILL_MCP_MCP_JSON_PATH", ".agents/mcp.local.json")
    monkeypatch.setattr(agent, "get_settings", lambda: SimpleNamespace(resolved_mcp_config_path=lambda: effective))
    monkeypatch.setattr(agent, "_patch_sdk_skill_for_null_category", lambda: None)
    monkeypatch.setattr(sdk, "load_agent_config", AsyncMock(return_value=SimpleNamespace(skills=[], mcps=[])))
    monkeypatch.setattr(sdk, "sync_skills", lambda _: [])
    monkeypatch.setattr(agent, "load_static_agent_assets", lambda _: None)
    sync = AsyncMock()
    monkeypatch.setattr(sdk, "sync_mcp_config", sync)
    asyncio.run(agent._sync_platform_config())
    sync.assert_not_called()


def test_effective_unreadable_behaves_as_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "get_settings", lambda: SimpleNamespace(resolved_mcp_config_path=lambda: tmp_path))
    assert tools._read_mcp_servers() == {}
