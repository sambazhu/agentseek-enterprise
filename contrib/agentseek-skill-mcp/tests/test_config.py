from __future__ import annotations

import agentseek_skill_mcp.config as config_mod
from agentseek_skill_mcp.config import SkillMCPSettings, get_platform_client


def test_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("AGENTSEEK_SKILL_MCP_ENABLED", raising=False)
    settings = SkillMCPSettings.from_env()
    assert settings.enabled is False


def test_enabled_truthy(monkeypatch) -> None:
    monkeypatch.setenv("AGENTSEEK_SKILL_MCP_ENABLED", "true")
    settings = SkillMCPSettings.from_env()
    assert settings.enabled is True


def test_base_url_and_api_key(monkeypatch) -> None:
    monkeypatch.setenv("AGENTSEEK_SKILL_MCP_BASE_URL", "https://skill.example.com")
    monkeypatch.setenv("AGENTSEEK_SKILL_MCP_API_KEY", "secret-key")
    settings = SkillMCPSettings.from_env()
    assert settings.base_url == "https://skill.example.com"
    assert settings.api_key == "secret-key"


def test_cache_ttl_default_and_custom(monkeypatch) -> None:
    # Default
    monkeypatch.delenv("AGENTSEEK_SKILL_MCP_CACHE_TTL", raising=False)
    assert SkillMCPSettings.from_env().cache_ttl == 300

    # Custom value
    monkeypatch.setenv("AGENTSEEK_SKILL_MCP_CACHE_TTL", "600")
    assert SkillMCPSettings.from_env().cache_ttl == 600

    # Invalid falls back to default
    monkeypatch.setenv("AGENTSEEK_SKILL_MCP_CACHE_TTL", "not-a-number")
    assert SkillMCPSettings.from_env().cache_ttl == 300


def test_skill_docs_dir_default(monkeypatch) -> None:
    monkeypatch.delenv("AGENTSEEK_SKILL_MCP_SKILL_DOCS_DIR", raising=False)
    settings = SkillMCPSettings.from_env()
    assert settings.skill_docs_dir == "skill_docs"


def test_mcp_json_path_default(monkeypatch) -> None:
    monkeypatch.delenv("AGENTSEEK_SKILL_MCP_MCP_JSON_PATH", raising=False)
    settings = SkillMCPSettings.from_env()
    assert settings.mcp_json_path == ".agents/mcp.json"


def test_get_platform_client_disabled(monkeypatch) -> None:
    config_mod._platform_client = None
    config_mod._platform_client_initialized = False
    monkeypatch.delenv("AGENTSEEK_SKILL_MCP_ENABLED", raising=False)

    assert get_platform_client() is None


def test_get_platform_client_missing_credentials(monkeypatch) -> None:
    config_mod._platform_client = None
    config_mod._platform_client_initialized = False
    monkeypatch.setenv("AGENTSEEK_SKILL_MCP_ENABLED", "true")
    monkeypatch.delenv("AGENTSEEK_SKILL_MCP_BASE_URL", raising=False)
    monkeypatch.delenv("AGENTSEEK_SKILL_MCP_API_KEY", raising=False)

    assert get_platform_client() is None
