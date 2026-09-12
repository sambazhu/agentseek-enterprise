"""Identity and capability discovery independent of the work ledger switch."""

from collections.abc import Mapping
from dataclasses import replace
from functools import lru_cache
from pathlib import Path

from enterprise_wecom_digital_employee.capability_catalog import (
    configured_mcp_server_names,
    resolve_runtime_capabilities,
)
from enterprise_wecom_digital_employee.channel_command import authenticated_user_command_text
from enterprise_wecom_digital_employee.job_charter import match_job_charter_intent, render_job_charter_response
from enterprise_wecom_digital_employee.pack_loader import RestrictedPackLoader
from enterprise_wecom_digital_employee.settings import PROJECT_ROOT, get_settings


@lru_cache(maxsize=1)
def load_discovery_profile():
    root = PROJECT_ROOT / "digital_employees" / "industry-report"
    return RestrictedPackLoader(
        pack_root=root,
        allowed_entrypoint_package="enterprise_wecom_digital_employee",
        asset_resolver=lambda ref: (
            root / "assets" / "neutral-industry-report-v1.docx"
            if ref == "trusted-asset://strategic-report-docx/1.0.0" else Path("/untrusted")
        ),
    ).load().profile


def identity_response(message: str, state: Mapping[str, object], *, profile=None) -> str | None:
    command = authenticated_user_command_text(message).strip().rstrip("。.!！?？")
    if command in {"我是谁", "我的身份是什么", "请介绍一下我的身份"}:
        employee = state.get("employee_context")
        if not isinstance(employee, Mapping) or not employee.get("name"):
            return "当前未能完整识别你的员工身份，请联系管理员核对企业微信身份绑定。"
        name = str(employee["name"])
        org = employee.get("org_path_label") or employee.get("dept_name")
        return f"你是{name}，所属组织：{org}。" if org else f"你是{name}。当前没有可用的组织信息。"
    intent = match_job_charter_intent(message)
    if intent is None:
        return None
    profile = profile or load_discovery_profile()
    settings = get_settings()
    availability = resolve_runtime_capabilities(
        profile,
        effective_tool_grants=frozenset(profile.tool_grants),
        effective_data_scopes=frozenset(profile.data_scopes),
        configured_servers=configured_mcp_server_names(settings.resolved_mcp_config_path()),
    )
    displayed_profile = profile if settings.work_enabled else replace(profile, service_catalog=())
    response = render_job_charter_response(displayed_profile, intent, capabilities=availability)
    if not settings.work_enabled:
        response += "\n当前正式报告工作流未启用；日常问答按已配置能力提供。"
    return response + production_capability_summary()


def production_capability_summary() -> str:
    settings = get_settings()
    if not settings.production_mcp_enabled:
        return ""
    servers = configured_mcp_server_names(settings.resolved_mcp_config_path())
    lines = []
    if "OA流程助手" in servers:
        lines.append("可尝试在私聊中问“查一下我今天的会议”或“我的报销进度”；个人查询使用当前已验证员工身份。")
    if "信息技术部知识库" in servers and (settings.it_policy_kb_code or settings.it_regulation_kb_code):
        lines.append("可询问信息技术部已配置的制度或外规，并查看检索来源。")
    if lines:
        lines.append("以上入口已配置，实际可用性还取决于接口连通性与访问策略。")
    return "\n" + "\n".join(lines) if lines else ""
