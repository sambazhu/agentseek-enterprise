from __future__ import annotations

import re
from enum import StrEnum

from enterprise_wecom_digital_employee.channel_command import authenticated_user_command_text
from enterprise_wecom_digital_employee.pack_loader import DigitalEmployeeProfile


class JobCharterIntent(StrEnum):
    IDENTITY = "identity"
    CAPABILITIES = "capabilities"
    USAGE = "usage"


_IDENTITY_COMMANDS = frozenset({
    "你是谁",
    "介绍一下你自己",
    "请介绍一下你自己",
    "你的身份是什么",
})
_CAPABILITY_COMMANDS = frozenset({
    "你能做什么",
    "你会做什么",
    "你有哪些能力",
    "有哪些服务",
    "你有哪些服务",
})
_USAGE_COMMANDS = frozenset({
    "怎么使用你",
    "如何使用你",
    "我该怎么使用你",
    "怎么开始",
    "如何开始",
})
_TRAILING_PUNCTUATION_RE = re.compile(r"[。.!！?？]+$")


def match_job_charter_intent(message: str) -> JobCharterIntent | None:
    """Match only a small deterministic set of employee-facing discovery commands."""

    command = _normalized_command(message)
    if command in _IDENTITY_COMMANDS:
        return JobCharterIntent.IDENTITY
    if command in _CAPABILITY_COMMANDS:
        return JobCharterIntent.CAPABILITIES
    if command in _USAGE_COMMANDS:
        return JobCharterIntent.USAGE
    return None


def render_job_charter_response(
    profile: DigitalEmployeeProfile,
    intent: JobCharterIntent,
) -> str:
    if intent is JobCharterIntent.IDENTITY:
        return _identity_response(profile)
    if intent is JobCharterIntent.CAPABILITIES:
        return _capabilities_response(profile)
    if intent is JobCharterIntent.USAGE:
        return _usage_response(profile)
    raise ValueError("unsupported Job Charter intent")


def _normalized_command(message: str) -> str:
    command = authenticated_user_command_text(message).strip()
    command = _TRAILING_PUNCTUATION_RE.sub("", command).strip()
    return re.sub(r"\s+", "", command)


def _identity_response(profile: DigitalEmployeeProfile) -> str:
    lines = [
        f"我是{profile.display_name}，编号 {profile.employee_code}，隶属{profile.owning_org}。",
        f"我的岗位使命是：{profile.mission}。",
    ]
    if profile.service_catalog:
        services = "、".join(service.title for service in profile.service_catalog)
        lines.append(f"当前正式服务包括：{services}。")
    if profile.behavior_principles:
        lines.append(f"我的工作准则是：{'；'.join(profile.behavior_principles)}。")
    lines.append("你可以回复“你能做什么”查看服务，或回复“怎么使用你”查看操作方式。")
    return "\n".join(lines)


def _capabilities_response(profile: DigitalEmployeeProfile) -> str:
    if not profile.service_catalog:
        responsibilities = "；".join(profile.responsibilities)
        return f"我的岗位职责包括：{responsibilities}。"

    lines = ["我目前提供以下正式服务："]
    for index, service in enumerate(profile.service_catalog, start=1):
        lines.append(f"{index}. {service.title}：{service.summary}。")
        lines.append(f"   工作过程：{' → '.join(service.workflow_steps)}。")
        lines.append(f"   示例：{service.example_requests[0]}")
    lines.append("正式流程中的关键版本需要你明确确认，未完成授权或审批时不会自动推进。")
    return "\n".join(lines)


def _usage_response(profile: DigitalEmployeeProfile) -> str:
    lines = [
        "你可以直接说明业务目标、报告主题、覆盖期和期望交付物。",
        "我会先判断请求属于普通协助还是正式 Playbook；存在歧义时会先请你选择，不会静默启动任务。",
    ]
    if profile.service_catalog:
        service = profile.service_catalog[0]
        lines.append(f"当前可以从这句话开始：{service.example_requests[0]}")
        lines.append(f"该服务按以下步骤推进：{' → '.join(service.workflow_steps)}。")
    if profile.behavior_principles:
        lines.append(f"执行边界：{'；'.join(profile.behavior_principles)}。")
    return "\n".join(lines)
