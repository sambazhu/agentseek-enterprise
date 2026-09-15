"""Private platform cache plus whole-server local overrides; never log payloads."""

import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def _validate(value, local):
    if not isinstance(value, dict) or not isinstance(value.get("mcpServers"), dict):
        raise ValueError
    servers = value["mcpServers"]
    if not all(isinstance(item, dict) or (local and item is None) for item in servers.values()):
        raise ValueError
    return servers


def _servers(path: Path, *, local: bool) -> dict:
    role = "local" if local else "dmcp"
    try:
        raw = path.read_text(encoding="utf-8")
        os.chmod(path, 0o600)
        if local and not raw.strip():
            return {}
        value = json.loads(raw)
        servers = _validate(value, local)
    except FileNotFoundError:
        if not local:
            logger.warning("mcp_merge dmcp_missing")
    except (OSError, ValueError):
        logger.warning("mcp_merge %s_invalid_or_unreadable", role)
    else:
        return servers
    return {}


def merge_mcp_config(dmcp: Path, local: Path, effective: Path) -> bool:
    """Return write success. Failures contain no paths, server names or secrets."""
    if len({p.resolve() for p in (dmcp, local, effective)}) != 3:
        logger.error("mcp_merge path_collision")
        return False
    platform = _servers(dmcp, local=False)
    overrides = _servers(local, local=True)
    merged = dict(platform)
    replaced = blocked = 0
    for name, server in overrides.items():
        if server is None:
            blocked += int(name in merged)
            merged.pop(name, None)
        else:
            replaced += int(name in merged)
            merged[name] = server
    temporary = None
    try:
        effective.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".mcp-", dir=effective.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump({"mcpServers": merged}, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, effective)
        temporary = None
    except OSError:
        logger.error("mcp_merge effective_write_failed")  # noqa: TRY400 -- exceptions may expose private paths
        return False
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                logger.warning("mcp_merge temporary_cleanup_failed")
    logger.info("mcp_merge dmcp=%d local=%d overrides=%d blocked=%d", len(platform), len(overrides), replaced, blocked)
    return True
