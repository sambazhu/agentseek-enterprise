"""Core Python API wrapping the ``agent-skill-mcp`` SDK.

All SDK imports are performed lazily inside the functions so a missing optional
dependency never breaks callers. Every function is defensive: on failure it logs
a warning and returns a safe default instead of raising.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from agentseek_skill_mcp.config import get_platform_client
from agentseek_skill_mcp.runtime_logging import get_logger

logger = get_logger(__name__)


async def load_agent_config(agent_name: str | None = None) -> Any | None:
    """Build an :class:`AgentConfig` from the platform, or ``None`` on failure.

    ``agent_name`` falls back to the ``AGENTSEEK_SKILL_MCP_AGENT_NAME`` env var.
    """
    client = get_platform_client()
    if client is None:
        return None

    resolved_name = agent_name or os.environ.get("AGENTSEEK_SKILL_MCP_AGENT_NAME", "")
    if not resolved_name:
        logger.warning("skill-mcp agent_name not configured; skipping agent config load")
        return None

    try:
        from agent_skill_mcp import AgentBuilder
    except ImportError:
        logger.warning("agent-skill-mcp SDK is not installed; cannot load agent config")
        return None

    try:
        return await AgentBuilder(client).build(resolved_name)
    except Exception as exc:  # pragma: no cover - defensive: SDK/network failures must not propagate.
        logger.warning("failed to build agent config for {}: {}: {}", resolved_name, type(exc).__name__, exc)
        print(f"[skill-mcp] load_agent_config error detail: {type(exc).__name__}: {exc}", flush=True)
        return None


def sync_skills(agent_config: Any, output_dir: str = "") -> list[str]:
    """Export skill docs to ``output_dir``, returning the written file paths."""
    if agent_config is None or not getattr(agent_config, "skills", None):
        return []

    resolved_dir = output_dir or os.environ.get("AGENTSEEK_SKILL_MCP_SKILL_DOCS_DIR", "skill_docs")
    client = get_platform_client()

    try:
        written = agent_config.export_skills_to_dir(resolved_dir, include_attachments=True, client=client)
    except Exception:  # pragma: no cover - defensive: SDK failures must not propagate.
        logger.warning("failed to export skills to {}", resolved_dir)
        return []

    # Post-process: move flat <skill_name>.md into <skill_name>/SKILL.md
    # so that LLM skill loaders can find the entry file inside the directory.
    _normalize_skill_layout(resolved_dir)

    logger.info("wrote {} skill files to {}", len(written), resolved_dir)
    return written


def _normalize_skill_layout(output_dir: str) -> None:
    """Move flat ``<name>.md`` skill files into ``<name>/SKILL.md``.

    The SDK ``export_skills_to_dir`` writes skill entry docs as flat files
    (``skills/ppt-master.md``) alongside attachment directories
    (``skills/ppt-master/``).  Many skill loaders expect ``<name>/SKILL.md``
    instead.  This function bridges the gap by relocating the flat entry into
    the attachment directory when the directory exists and lacks a SKILL.md.
    """
    base = Path(output_dir)
    if not base.is_dir():
        return

    for md_file in base.glob("*.md"):
        skill_name = md_file.stem  # e.g. "ppt-master"
        skill_dir = base / skill_name
        target = skill_dir / "SKILL.md"

        # Only move if the attachment directory exists and SKILL.md is missing
        if skill_dir.is_dir() and not target.exists():
            try:
                md_file.replace(target)
                logger.info("normalized skill layout: {} -> {}", md_file.name, f"{skill_name}/SKILL.md")
            except OSError:
                pass  # best-effort; leave flat if move fails


async def sync_mcp_config(agent_config: Any, mcp_json_path: str = "") -> dict[str, Any]:
    """Build the MCP servers config and write it to ``mcp_json_path``.

    Returns the ``{"mcpServers": ...}`` dict (empty on failure). Only
    ``MCPConfigAdapter.build_from_mcps()`` is called — never ``load_tools()``.
    """
    if agent_config is None or not getattr(agent_config, "mcps", None):
        return {}

    try:
        from agent_skill_mcp import MCPConfigAdapter
    except ImportError:
        logger.warning("agent-skill-mcp SDK is not installed; cannot sync MCP config")
        return {}

    try:
        sdk_config = await MCPConfigAdapter().build_from_mcps(agent_config.mcps)
    except Exception:  # pragma: no cover - defensive: SDK failures must not propagate.
        logger.warning("failed to build MCP config from agent mcps")
        return {}

    converted = _convert_to_mcp_servers_format(sdk_config)

    resolved_path = mcp_json_path or os.environ.get("AGENTSEEK_SKILL_MCP_MCP_JSON_PATH", ".agents/mcp.json")
    temporary = None
    try:
        path = Path(resolved_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".mcp-cache-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(converted, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except Exception:  # pragma: no cover - defensive: filesystem failures must not propagate.
        logger.warning("failed to write MCP config to {}", resolved_path)
        return {}
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                logger.warning("MCP cache temporary cleanup failed")

    server_count = len(converted.get("mcpServers", {}))
    logger.info("wrote {} MCP servers to {}", server_count, resolved_path)
    return converted


def build_skill_system_prompt(agent_config: Any, base_prompt: str = "") -> str:
    """Return a system prompt augmented with skill descriptions."""
    if agent_config is None:
        return base_prompt

    try:
        return agent_config.build_system_prompt(base_prompt)
    except Exception:  # pragma: no cover - defensive: SDK failures must not propagate.
        logger.warning("failed to build skill system prompt")
        return base_prompt


def resolve_model_config(agent_config: Any) -> dict[str, Any] | None:
    """Resolve the model config dict from the agent config, or ``None``."""
    model_config = getattr(agent_config, "model_config", None) if agent_config is not None else None
    if not model_config:
        return None

    client = get_platform_client()
    if client is None:
        logger.warning("skill-mcp platform client not available; cannot resolve model config")
        return None

    try:
        from agent_skill_mcp import ModelConfigAdapter
    except ImportError:
        logger.warning("agent-skill-mcp SDK is not installed; cannot resolve model config")
        return None

    try:
        return ModelConfigAdapter(client).build_from_agent_config(model_config)
    except Exception:  # pragma: no cover - defensive: SDK failures must not propagate.
        logger.warning("failed to resolve model config")
        return None


def _convert_to_mcp_servers_format(sdk_config: dict[str, dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    """Convert SDK MCP config to .agents/mcp.json format."""
    servers: dict[str, dict[str, Any]] = {}
    for name, config in sdk_config.items():
        cleaned = {k: v for k, v in config.items() if k != "transport"}
        servers[name] = cleaned
    return {"mcpServers": servers}
