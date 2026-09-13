"""Small, unambiguous controls; never infer cancellation from quoted prose."""

import re

from {{ cookiecutter.project_slug }}.channel_command import authenticated_user_command_text

_CONFIRM_AND_DRAFT = re.compile(
    r"确认\s*ReportBrief\s*v(\d+)\s*并自动研究生成初稿", re.IGNORECASE,
)


def explicitly_cancels_current_work(message: str) -> bool:
    command = authenticated_user_command_text(message).strip().rstrip("。.!！")
    return command in {
        "取消当前任务", "取消当前报告", "取消报告任务", "取消当前报告任务",
        "请取消当前任务", "请取消当前报告", "请取消当前报告任务",
    }


def explicitly_creates_new_report(message: str) -> bool:
    """Bounded new-task consent; bare agreement, revision, quotes and questions fail closed."""
    command = authenticated_user_command_text(message).strip().rstrip("。.!！")
    return command in {
        f"{prefix}{verb}{target}"
        for prefix in ("", "请", "确认", "同意")
        for verb in ("新建", "创建新的", "创建一个新的", "新建一个", "新建一份")
        for target in ("报告", "报告任务", "行业报告", "行业报告任务")
    }


def requests_automatic_draft(message: str) -> bool:
    command = authenticated_user_command_text(message).strip().rstrip("。.!！")
    return automatic_draft_brief_version(message) is not None or command in {
        "按已确认需求自动研究并生成初稿",
        "请按已确认需求自动研究并生成初稿",
    }


def automatic_draft_brief_version(message: str) -> int | None:
    command = authenticated_user_command_text(message).strip().rstrip("。.!！")
    match = _CONFIRM_AND_DRAFT.fullmatch(command)
    return int(match[1]) if match else None
