from __future__ import annotations

from pathlib import Path

from enterprise_wecom_digital_employee.capability_catalog import (
    configured_mcp_server_names,
    resolve_runtime_capabilities,
)
from enterprise_wecom_digital_employee.pack_loader import PackLoadError, RestrictedPackLoader

PROJECT_ROOT = Path(__file__).parents[1]
PACK_ROOT = PROJECT_ROOT / "digital_employees" / "industry-report"
ASSET_REF = "trusted-asset://strategic-report-docx/1.0.0"


def _load_profile():
    def resolve_asset(artifact_ref: str) -> Path:
        if artifact_ref != ASSET_REF:
            raise PackLoadError("unknown trusted asset")
        return PACK_ROOT / "assets" / "neutral-industry-report-v1.docx"

    return RestrictedPackLoader(
        pack_root=PACK_ROOT,
        allowed_entrypoint_package="enterprise_wecom_digital_employee",
        asset_resolver=resolve_asset,
    ).load().profile


def test_configured_mcp_server_names_reads_identifiers_only_and_fails_closed(tmp_path: Path) -> None:
    config = tmp_path / "mcp.json"
    config.write_text(
        '{"mcpServers":{"department-knowledge":{"command":"secret"},'
        '"tavily-search":{"url":"https://example.invalid"},"invalid":null}}',
        encoding="utf-8",
    )

    assert configured_mcp_server_names(config) == frozenset({
        "department-knowledge",
        "tavily-search",
    })

    config.write_text("not-json", encoding="utf-8")
    assert configured_mcp_server_names(config) == frozenset()
    assert configured_mcp_server_names(tmp_path / "missing.json") == frozenset()


def test_runtime_capabilities_intersect_profile_playbook_and_deployment() -> None:
    profile = _load_profile()

    capabilities = resolve_runtime_capabilities(
        profile,
        effective_tool_grants=frozenset({
            "analyze_file",
            "department-knowledge-read",
            "gildata-read",
        }),
        effective_data_scopes=frozenset({
            "gildata-licensed-data",
            "authorized-public-sources",
        }),
        configured_servers=frozenset({"department-knowledge", "tavily-search"}),
    )

    assert capabilities.file_analysis is True
    assert capabilities.department_knowledge is True
    assert capabilities.public_search is True
    assert capabilities.licensed_external_data is False


def test_runtime_capabilities_do_not_claim_unconfigured_or_ungranted_services() -> None:
    profile = _load_profile()

    capabilities = resolve_runtime_capabilities(
        profile,
        effective_tool_grants=frozenset({"analyze_file", "department-knowledge-read"}),
        effective_data_scopes=frozenset({
            "gildata-licensed-data",
            "authorized-public-sources",
        }),
        configured_servers=frozenset({"gildata_datamap-data", "tavily-search"}),
    )

    assert capabilities.file_analysis is True
    assert capabilities.department_knowledge is False
    assert capabilities.licensed_external_data is False
    assert capabilities.public_search is True
