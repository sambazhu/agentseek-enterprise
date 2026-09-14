from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from agentseek_skill_mcp.loader import (
    build_skill_system_prompt,
    load_agent_config,
    resolve_model_config,
    sync_mcp_config,
    sync_skills,
)

# ---------------------------------------------------------------------------
# load_agent_config
# ---------------------------------------------------------------------------


def test_load_agent_config_success() -> None:
    mock_client = MagicMock()
    config_obj = SimpleNamespace(skills=[], mcps=[], model_config=None)
    mock_agent = MagicMock()
    mock_agent.build = AsyncMock(return_value=config_obj)
    fake_builder = MagicMock(return_value=mock_agent)
    fake_module = MagicMock(AgentBuilder=fake_builder)

    with (
        patch("agentseek_skill_mcp.loader.get_platform_client", return_value=mock_client),
        patch.dict(sys.modules, {"agent_skill_mcp": fake_module}),
    ):
        result = asyncio.run(load_agent_config("my-agent"))

    assert result is config_obj
    fake_builder.assert_called_once_with(mock_client)


def test_load_agent_config_no_client() -> None:
    with patch("agentseek_skill_mcp.loader.get_platform_client", return_value=None):
        result = asyncio.run(load_agent_config("my-agent"))

    assert result is None


def test_load_agent_config_platform_error() -> None:
    mock_client = MagicMock()
    mock_agent = MagicMock()
    mock_agent.build = AsyncMock(side_effect=RuntimeError("boom"))
    fake_builder = MagicMock(return_value=mock_agent)
    fake_module = MagicMock(AgentBuilder=fake_builder)

    with (
        patch("agentseek_skill_mcp.loader.get_platform_client", return_value=mock_client),
        patch.dict(sys.modules, {"agent_skill_mcp": fake_module}),
    ):
        result = asyncio.run(load_agent_config("my-agent"))

    assert result is None


# ---------------------------------------------------------------------------
# sync_skills
# ---------------------------------------------------------------------------


def test_sync_skills_success(tmp_path: Path) -> None:
    mock_client = MagicMock()
    written = [str(tmp_path / "a.md"), str(tmp_path / "b.md")]
    agent_config = MagicMock()
    agent_config.skills = [MagicMock()]
    agent_config.export_skills_to_dir = MagicMock(return_value=written)

    with patch("agentseek_skill_mcp.loader.get_platform_client", return_value=mock_client):
        result = sync_skills(agent_config, str(tmp_path))

    assert result == written
    agent_config.export_skills_to_dir.assert_called_once_with(
        str(tmp_path), include_attachments=True, client=mock_client
    )


def test_sync_skills_no_skills() -> None:
    agent_config = MagicMock()
    agent_config.skills = []

    result = sync_skills(agent_config)

    assert result == []


# ---------------------------------------------------------------------------
# sync_mcp_config
# ---------------------------------------------------------------------------


def test_sync_mcp_config_success(tmp_path: Path) -> None:
    sdk_config = {
        "tavily": {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "tavily-mcp"],
            "env": {"KEY": "val"},
        }
    }
    mock_adapter = MagicMock()
    mock_adapter.build_from_mcps = AsyncMock(return_value=sdk_config)
    fake_module = MagicMock(
        MCPConfigAdapter=MagicMock(return_value=mock_adapter),
        load_tools=MagicMock(),
    )

    agent_config = MagicMock()
    agent_config.mcps = [MagicMock()]

    mcp_path = str(tmp_path / ".agents" / "mcp.json")

    with patch.dict(sys.modules, {"agent_skill_mcp": fake_module}):
        result = asyncio.run(sync_mcp_config(agent_config, mcp_path))

    expected = {
        "mcpServers": {
            "tavily": {
                "command": "npx",
                "args": ["-y", "tavily-mcp"],
                "env": {"KEY": "val"},
            }
        }
    }
    assert result == expected

    written = json.loads(Path(mcp_path).read_text(encoding="utf-8"))
    assert written == expected

    fake_module.load_tools.assert_not_called()


def test_sync_mcp_config_no_mcps() -> None:
    agent_config = MagicMock()
    agent_config.mcps = []

    result = asyncio.run(sync_mcp_config(agent_config))

    assert result == {}


# ---------------------------------------------------------------------------
# build_skill_system_prompt
# ---------------------------------------------------------------------------


def test_build_skill_system_prompt_success() -> None:
    agent_config = MagicMock()
    agent_config.build_system_prompt = MagicMock(return_value="base\n\n[Skills]")

    result = build_skill_system_prompt(agent_config, "base")

    assert result == "base\n\n[Skills]"


def test_build_skill_system_prompt_none_config() -> None:
    result = build_skill_system_prompt(None, "base prompt")

    assert result == "base prompt"


# ---------------------------------------------------------------------------
# resolve_model_config
# ---------------------------------------------------------------------------


def test_resolve_model_config_success() -> None:
    mock_client = MagicMock()
    model_config = {"model": "gpt-4"}
    agent_config = SimpleNamespace(model_config=model_config)
    resolved = {"provider": "openai", "model": "gpt-4"}
    mock_adapter = MagicMock()
    mock_adapter.build_from_agent_config = MagicMock(return_value=resolved)
    fake_module = MagicMock(ModelConfigAdapter=MagicMock(return_value=mock_adapter))

    with (
        patch("agentseek_skill_mcp.loader.get_platform_client", return_value=mock_client),
        patch.dict(sys.modules, {"agent_skill_mcp": fake_module}),
    ):
        result = resolve_model_config(agent_config)

    assert result == resolved
    fake_module.ModelConfigAdapter.assert_called_once_with(mock_client)


def test_resolve_model_config_empty() -> None:
    agent_config = SimpleNamespace(model_config=None)

    result = resolve_model_config(agent_config)

    assert result is None


def test_resolve_model_config_no_client() -> None:
    agent_config = SimpleNamespace(model_config={"model": "gpt-4"})

    with patch("agentseek_skill_mcp.loader.get_platform_client", return_value=None):
        result = resolve_model_config(agent_config)

    assert result is None
