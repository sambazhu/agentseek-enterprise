from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from {{ cookiecutter.project_slug }}.channel_command import authenticated_user_command_text
from {{ cookiecutter.project_slug }}.pack_loader import PlaybookSpec


class PlaybookRouteStatus(StrEnum):
    SELECTED = "selected"
    CLARIFICATION_REQUIRED = "clarification_required"
    OUT_OF_SCOPE = "out_of_scope"
    FORBIDDEN = "forbidden"


class PlaybookRouteReason(StrEnum):
    ACTIVE_WORK = "active_work"
    EXACT_ACTION = "exact_action"
    EXPLICIT_SERVICE = "explicit_service"
    DETERMINISTIC_MATCH = "deterministic_match"
    AMBIGUOUS = "ambiguous"
    NO_MATCH = "no_match"
    REQUESTER_FORBIDDEN = "requester_forbidden"


@dataclass(frozen=True, slots=True)
class PlaybookRouteResult:
    status: PlaybookRouteStatus
    reason_code: PlaybookRouteReason
    selected_playbook_ref: str | None = None
    candidate_playbook_refs: tuple[str, ...] = ()

    def to_state(self) -> dict[str, object]:
        return {
            "route_status": self.status.value,
            "reason_code": self.reason_code.value,
            "playbook_ref": self.selected_playbook_ref or "",
            "candidate_playbook_refs": list(self.candidate_playbook_refs),
        }


_CONTINUATION_COMMANDS = frozenset({
    "继续",
    "继续处理",
    "继续当前任务",
    "查看当前任务",
    "查看当前任务状态",
    "当前任务",
    "当前任务状态",
    "任务进度",
})
_EXPLICIT_PREFIXES = ("使用", "选择", "进入", "启动")
_FORMAL_REQUEST_TERMS = ("正式", "报告", "任务", "审批", "发布", "交付")
_TRAILING_PUNCTUATION_RE = re.compile(r"[。.!！?？]+$")


def route_playbook(
    message: str,
    *,
    playbooks: Sequence[PlaybookSpec],
    active_playbook_refs: Sequence[str] = (),
    requester_allowed: bool = True,
) -> PlaybookRouteResult:
    """Route one employee message without a model or probabilistic classifier."""

    if not requester_allowed:
        return PlaybookRouteResult(
            status=PlaybookRouteStatus.FORBIDDEN,
            reason_code=PlaybookRouteReason.REQUESTER_FORBIDDEN,
        )

    command = _normalized_command(message)
    ordered = tuple(sorted(playbooks, key=lambda item: (-item.routing.priority, item.ref)))

    exact = tuple(
        item.ref
        for item in ordered
        if any(_normalized_term(term) in command for term in item.routing.owned_command_terms)
    )
    if exact:
        return _candidate_result(exact, reason=PlaybookRouteReason.EXACT_ACTION)

    explicit = tuple(
        item.ref
        for item in ordered
        if any(_explicitly_selects(command, alias) for alias in item.routing.explicit_aliases)
    )
    if explicit:
        return _candidate_result(explicit, reason=PlaybookRouteReason.EXPLICIT_SERVICE)

    active = tuple(reference for reference in active_playbook_refs if reference in {item.ref for item in ordered})
    if command in _CONTINUATION_COMMANDS and active:
        return _candidate_result(active, reason=PlaybookRouteReason.ACTIVE_WORK)

    matched = tuple(
        item.ref
        for item in ordered
        if any(_normalized_term(term) in command for term in item.routing.intent_terms)
    )
    if matched:
        return _candidate_result(matched, reason=PlaybookRouteReason.DETERMINISTIC_MATCH)

    if any(term in command for term in _FORMAL_REQUEST_TERMS):
        return PlaybookRouteResult(
            status=PlaybookRouteStatus.CLARIFICATION_REQUIRED,
            reason_code=PlaybookRouteReason.NO_MATCH,
            candidate_playbook_refs=tuple(item.ref for item in ordered),
        )

    return PlaybookRouteResult(
        status=PlaybookRouteStatus.OUT_OF_SCOPE,
        reason_code=PlaybookRouteReason.NO_MATCH,
    )


def render_route_clarification(
    result: PlaybookRouteResult,
    *,
    service_titles: Mapping[str, str],
) -> str | None:
    if result.status is not PlaybookRouteStatus.CLARIFICATION_REQUIRED:
        return None
    lines = ["这条请求可能对应多个正式服务，请明确选择一个："]
    for index, reference in enumerate(result.candidate_playbook_refs, start=1):
        lines.append(f"{index}. {service_titles.get(reference, reference)}")
    lines.append("请回复“使用 + 服务名称 + 你的具体需求”。在你选择前，我不会启动任务。")
    return "\n".join(lines)


def _candidate_result(
    candidates: Sequence[str],
    *,
    reason: PlaybookRouteReason,
) -> PlaybookRouteResult:
    unique = tuple(dict.fromkeys(candidates))
    if len(unique) == 1:
        return PlaybookRouteResult(
            status=PlaybookRouteStatus.SELECTED,
            reason_code=reason,
            selected_playbook_ref=unique[0],
            candidate_playbook_refs=unique,
        )
    return PlaybookRouteResult(
        status=PlaybookRouteStatus.CLARIFICATION_REQUIRED,
        reason_code=PlaybookRouteReason.AMBIGUOUS,
        candidate_playbook_refs=unique,
    )


def _normalized_command(message: str) -> str:
    command = authenticated_user_command_text(message).strip().lower()
    command = _TRAILING_PUNCTUATION_RE.sub("", command).strip()
    return re.sub(r"\s+", "", command)


def _normalized_term(term: str) -> str:
    return re.sub(r"\s+", "", term.strip().lower())


def _explicitly_selects(command: str, alias: str) -> bool:
    normalized_alias = _normalized_term(alias)
    return any(f"{prefix}{normalized_alias}" in command for prefix in _EXPLICIT_PREFIXES)
