from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

from enterprise_wecom_digital_employee.capability_registry import (
    build_capability_registry,
    explicitly_authorizes_external_capability,
)
from enterprise_wecom_digital_employee.pack_loader import (
    PackLoadError,
    RestrictedPackLoader,
)
from langgraph.prebuilt import ToolRuntime

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


def _runtime(message: str, *, selected: bool = False) -> Any:
    state: dict[str, object] = {"latest_user_message": message}
    if selected:
        state["playbook_route"] = {
            "route_status": "selected",
            "playbook_ref": "securities-industry-report@1",
        }
    return ToolRuntime(
        state=state,
        context={},
        config={},
        stream_writer=lambda _chunk: None,
        tool_call_id=None,
        store=None,
    )


def test_registry_exposes_only_profile_owned_and_configured_capabilities(tmp_path: Path) -> None:
    config = tmp_path / "mcp.json"
    config.write_text(
        '{"mcpServers":{"department-knowledge":{},"tavily-search":{},'
        '"gildata_datamap-data":{}}}',
        encoding="utf-8",
    )

    registry = build_capability_registry(_load_profile(), mcp_config_path=config)
    names = {item.name for item in registry.tools}

    assert {
        "analyze_file",
        "list_department_knowledge",
        "search_department_knowledge",
        "read_department_knowledge",
        "search_licensed_external_data",
        "search_public_information",
    } <= names
    assert "call_mcp_tool" not in names
    assert "list_mcp_tools" not in names


def test_registry_hides_unconfigured_mcp_capabilities(tmp_path: Path) -> None:
    config = tmp_path / "mcp.json"
    config.write_text('{"mcpServers":{}}', encoding="utf-8")

    registry = build_capability_registry(_load_profile(), mcp_config_path=config)
    names = {item.name for item in registry.tools}

    assert "analyze_file" in names
    assert "search_department_knowledge" not in names
    assert "search_licensed_external_data" not in names
    assert "search_public_information" not in names


def test_direct_capability_uses_fixed_mcp_mapping_and_formal_route_fails_closed(
    tmp_path: Path,
) -> None:
    config = tmp_path / "mcp.json"
    config.write_text(
        '{"mcpServers":{"department-knowledge":{},"tavily-search":{},'
        '"gildata_datamap-data":{}}}',
        encoding="utf-8",
    )
    calls: list[tuple[str, str, dict[str, Any] | None, bool]] = []

    async def invoke(
        server: str,
        tool_name: str,
        arguments: dict[str, Any] | None,
        confirmed: bool,
    ) -> str:
        calls.append((server, tool_name, arguments, confirmed))
        return "ok"

    registry = build_capability_registry(
        _load_profile(),
        mcp_config_path=config,
        invoke_mcp=invoke,
    )
    tools = {item.name: item for item in registry.tools}

    result = asyncio.run(
        cast(Any, tools["search_department_knowledge"]).coroutine(
            query="证券行业数字化转型",
            runtime=_runtime("请检索部门知识"),
            search_mode="hybrid",
            top_k=6,
        )
    )
    public_result = asyncio.run(
        cast(Any, tools["search_public_information"]).coroutine(
            query="证券行业公开信息",
            runtime=_runtime("请使用公开搜索补充"),
            max_results=5,
        )
    )
    refused = asyncio.run(
        cast(Any, tools["search_public_information"]).coroutine(
            query="证券行业公开信息",
            runtime=_runtime("请使用公开搜索补充", selected=True),
            max_results=5,
        )
    )

    assert result == "ok"
    assert public_result == "ok"
    assert calls == [
        (
            "department-knowledge",
            "knowledge_search",
            {
                "query": "证券行业数字化转型",
                "search_mode": "hybrid",
                "top_k": 6,
            },
            False,
        ),
        (
            "tavily-search",
            "tavily_search",
            {
                "query": "证券行业公开信息",
                "max_results": 5,
                "search_depth": "advanced",
            },
            True,
        ),
    ]
    assert "正式 Playbook" in refused


def test_external_capabilities_require_latest_explicit_employee_request() -> None:
    assert explicitly_authorizes_external_capability(
        "请使用 Gildata 查询证券行业数据",
        capability="licensed_data",
    )
    assert explicitly_authorizes_external_capability(
        "请用公开搜索查找相关资料",
        capability="public_search",
    )
    assert not explicitly_authorizes_external_capability(
        "可以考虑公开信息",
        capability="public_search",
    )
    assert not explicitly_authorizes_external_capability(
        "不要使用 Tavily 搜索",
        capability="public_search",
    )
