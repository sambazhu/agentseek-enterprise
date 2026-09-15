import json
import logging

import pytest
from enterprise_wecom_digital_employee import mcp_config_merge as module


@pytest.mark.parametrize("platform,local,expected", [
    ({"a": {"url": "old", "env": {"key": "secret"}}}, {"a": {"url": "new"}}, {"a": {"url": "new"}}),
    ({"a": {}}, None, {"a": {}}),
    (None, {"b": {}}, {"b": {}}),
    (None, None, {}),
    ({"a": {}, "b": {}}, {"a": None}, {"b": {}}),
])
def test_whole_server_merge(tmp_path, caplog, platform, local, expected):
    dmcp, override, output = (tmp_path / name for name in ("dmcp", "local", "effective"))
    for path, value in ((dmcp, platform), (override, local)):
        if value is not None:
            path.write_text(json.dumps({"mcpServers": value}))
    caplog.set_level(logging.INFO)
    assert module.merge_mcp_config(dmcp, override, output)
    assert json.loads(output.read_text()) == {"mcpServers": expected}
    assert '\n  "mcpServers":' in output.read_text()
    assert output.stat().st_mode & 0o777 == 0o600
    for path in (dmcp, override):
        if path.exists():
            assert path.stat().st_mode & 0o777 == 0o600
    assert "secret" not in caplog.text and "url" not in caplog.text


@pytest.mark.parametrize("raw", ["", "  ", "broken-secret", '{"mcpServers": []}', '{"mcpServers":{"x":1}}'])
def test_invalid_or_empty_local_keeps_cache(tmp_path, caplog, raw):
    dmcp, local, output = (tmp_path / name for name in ("dmcp", "local", "effective"))
    dmcp.write_text('{"mcpServers":{"a":{}}}')
    local.write_text(raw)
    assert module.merge_mcp_config(dmcp, local, output)
    assert json.loads(output.read_text()) == {"mcpServers": {"a": {}}}
    assert "broken-secret" not in caplog.text


def test_replace_failure_preserves_previous_and_cleans_temporary(tmp_path, monkeypatch):
    dmcp, local, output = (tmp_path / name for name in ("dmcp", "local", "effective"))
    output.write_text("old")
    def fail(*args):
        raise OSError
    monkeypatch.setattr(module.os, "replace", fail)
    assert not module.merge_mcp_config(dmcp, local, output)
    assert output.read_text() == "old"
    assert list(tmp_path.iterdir()) == [output]


def test_path_collision_never_overwrites_local(tmp_path):
    local = tmp_path / "local"
    local.write_text("original")
    assert not module.merge_mcp_config(tmp_path / "dmcp", local, local)
    assert local.read_text() == "original"
