from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from {{ cookiecutter.project_slug }}.pack_loader import DigitalEmployeeProfile


@dataclass(frozen=True, slots=True)
class RuntimeCapabilityAvailability:
    file_analysis: bool
    department_knowledge: bool
    licensed_external_data: bool
    public_search: bool


def configured_mcp_server_names(config_path: Path) -> frozenset[str]:
    """Read configured server identifiers without connecting or exposing config values."""

    try:
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return frozenset()
    if not isinstance(loaded, dict):
        return frozenset()
    servers = loaded.get("mcpServers")
    if not isinstance(servers, dict):
        return frozenset()
    return frozenset(
        str(name).strip()
        for name, config in servers.items()
        if str(name).strip() and isinstance(config, dict)
    )


def resolve_runtime_capabilities(
    profile: DigitalEmployeeProfile,
    *,
    effective_tool_grants: frozenset[str],
    effective_data_scopes: frozenset[str],
    configured_servers: frozenset[str],
) -> RuntimeCapabilityAvailability:
    """Intersect Profile declarations, Playbook permissions, and deployment config."""

    knowledge_servers = {reference.server for reference in profile.knowledge_refs}
    return RuntimeCapabilityAvailability(
        file_analysis="analyze_file" in effective_tool_grants,
        department_knowledge=(
            "department-knowledge-read" in effective_tool_grants
            and bool(knowledge_servers & configured_servers)
        ),
        licensed_external_data=(
            "gildata-read" in effective_tool_grants
            and "gildata-licensed-data" in effective_data_scopes
            and "gildata_datamap-data" in configured_servers
        ),
        public_search=(
            "authorized-public-sources" in effective_data_scopes
            and "tavily-search" in configured_servers
        ),
    )


def profile_declared_capabilities(
    profile: DigitalEmployeeProfile,
) -> RuntimeCapabilityAvailability:
    """Compatibility view for callers without a deployment capability snapshot."""

    return RuntimeCapabilityAvailability(
        file_analysis="analyze_file" in profile.tool_grants,
        department_knowledge=bool(profile.knowledge_refs),
        licensed_external_data=(
            "gildata-read" in profile.tool_grants
            and "gildata-licensed-data" in profile.data_scopes
        ),
        public_search="authorized-public-sources" in profile.data_scopes,
    )
