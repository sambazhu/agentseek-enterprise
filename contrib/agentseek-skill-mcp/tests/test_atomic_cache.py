import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agentseek_skill_mcp import loader


@pytest.mark.parametrize("mode", ["ok", "fetch", "replace"])
def test_atomic_cache_preserves_last_good_on_failure(tmp_path, monkeypatch, mode):
    path = tmp_path / "cache"
    path.write_text('{"mcpServers":{"old":{}}}')
    before = path.read_bytes()
    build = AsyncMock(return_value={"new": {"transport": "http", "url": "https://example.invalid"}})
    if mode == "fetch":
        build.side_effect = RuntimeError("private-secret")
    monkeypatch.setitem(sys.modules, "agent_skill_mcp", SimpleNamespace(MCPConfigAdapter=lambda: SimpleNamespace(build_from_mcps=build)))
    if mode == "replace":
        def fail(*args):
            raise OSError
        monkeypatch.setattr(loader.os, "replace", fail)
    asyncio.run(loader.sync_mcp_config(SimpleNamespace(mcps=[object()]), mcp_json_path=str(path)))
    if mode == "ok":
        assert "new" in json.loads(path.read_text())["mcpServers"]
        assert path.stat().st_mode & 0o777 == 0o600
    else:
        assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]
