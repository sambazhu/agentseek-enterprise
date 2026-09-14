"""Configuration management for the skill-mcp plugin."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from agentseek_skill_mcp.runtime_logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class SkillMCPSettings:
    """Settings for the skill-mcp plugin.

    All knobs are driven by ``AGENTSEEK_SKILL_MCP_*`` environment variables.
    """

    enabled: bool = False
    base_url: str = ""
    api_key: str = ""
    agent_name: str = ""
    cache_ttl: int = 300
    skill_docs_dir: str = "skill_docs"
    mcp_json_path: str = ".agents/mcp.json"

    @classmethod
    def from_env(cls) -> SkillMCPSettings:
        return cls(
            enabled=_truthy(os.environ.get("AGENTSEEK_SKILL_MCP_ENABLED")),
            base_url=os.environ.get("AGENTSEEK_SKILL_MCP_BASE_URL", ""),
            api_key=os.environ.get("AGENTSEEK_SKILL_MCP_API_KEY", ""),
            agent_name=os.environ.get("AGENTSEEK_SKILL_MCP_AGENT_NAME", ""),
            cache_ttl=_safe_int(os.environ.get("AGENTSEEK_SKILL_MCP_CACHE_TTL"), 300),
            skill_docs_dir=os.environ.get("AGENTSEEK_SKILL_MCP_SKILL_DOCS_DIR", "skill_docs"),
            mcp_json_path=os.environ.get("AGENTSEEK_SKILL_MCP_MCP_JSON_PATH", ".agents/mcp.json"),
        )


_platform_client: Any | None = None
_platform_client_initialized: bool = False


def get_platform_client() -> Any | None:
    """Return a cached :class:`PlatformClient`, or ``None`` when unavailable.

    The client is created once and cached for the lifetime of the process.
    Creation is skipped when the plugin is disabled or required credentials
    are missing; the ``agent-skill-mcp`` SDK import is performed lazily so a
    missing optional dependency never breaks startup.
    """
    global _platform_client, _platform_client_initialized

    if _platform_client_initialized:
        return _platform_client

    settings = SkillMCPSettings.from_env()

    if not settings.enabled:
        _platform_client_initialized = True
        return None

    if not settings.base_url or not settings.api_key:
        logger.warning("skill-mcp enabled but base_url or api_key is empty; platform client unavailable")
        _platform_client_initialized = True
        return None

    try:
        from agent_skill_mcp import PlatformClient
    except ImportError:
        logger.warning("agent-skill-mcp SDK is not installed; platform client unavailable")
        _platform_client_initialized = True
        return None

    try:
        client = PlatformClient(settings.base_url, settings.api_key, settings.cache_ttl)
    except Exception:  # pragma: no cover - defensive: SDK failures must not break startup.
        logger.warning("failed to create PlatformClient; platform client unavailable")
        _platform_client_initialized = True
        return None

    _platform_client = client
    _platform_client_initialized = True
    return _platform_client


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _safe_int(value: str | None, default: int) -> int:
    try:
        return int(str(value or "").strip())
    except ValueError:
        return default
