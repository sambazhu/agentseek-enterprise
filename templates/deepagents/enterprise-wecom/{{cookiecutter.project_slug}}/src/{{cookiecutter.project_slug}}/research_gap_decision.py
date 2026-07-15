from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any

from agentseek_work import WorkContractSnapshot, WorkContractStatus

RESEARCH_GAP_DECISION_CONTRACT_TYPE = "report-research-gap-decision"

_REPORT_BRIEF_VERSION_RE = re.compile(
    r"(?:report\s*brief|reportbrief|报告简报)\s*(?:v|version|第)?\s*(\d+)\s*(?:版)?",
    re.IGNORECASE,
)
_NEGATION_RE = re.compile(
    r"(?:不要|不允许|不(?:再)?使用|不搜索|取消|暂不|不同意|禁止|do\s+not|don't)",
    re.IGNORECASE,
)
_QUESTION_RE = re.compile(r"(?:是否|能否|可以吗|吗[?？]?$|[?？])", re.IGNORECASE)


class ResearchGapAction(StrEnum):
    GILDATA = "gildata"
    PUBLIC_WEB = "public_web"
    UPLOAD_MATERIALS = "upload_materials"
    CONTINUE_WITH_GAPS = "continue_with_gaps"


_ACTION_PATTERNS = {
    ResearchGapAction.GILDATA: re.compile(r"(?:gildata|聚源)", re.IGNORECASE),
    ResearchGapAction.PUBLIC_WEB: re.compile(
        r"(?:tavily|公开(?:网络|网页)?搜索|联网搜索|网络搜索)",
        re.IGNORECASE,
    ),
    ResearchGapAction.UPLOAD_MATERIALS: re.compile(r"(?:上传|补充).{0,8}(?:材料|文件)", re.IGNORECASE),
    ResearchGapAction.CONTINUE_WITH_GAPS: re.compile(
        r"(?:保留|带着?).{0,6}缺口.{0,8}(?:继续|生成)|不再搜索.{0,8}(?:继续|生成)",
        re.IGNORECASE,
    ),
}


@dataclass(frozen=True, slots=True)
class ResearchGapDecision:
    report_brief_version: int
    research_plan_digest: str
    gap_digest: str
    gap_question_ids: tuple[str, ...]
    action: ResearchGapAction
    authorization_message_digest: str

    def __post_init__(self) -> None:
        if self.report_brief_version <= 0:
            raise ValueError("report_brief_version must be greater than zero")
        for field_name in (
            "research_plan_digest",
            "gap_digest",
            "authorization_message_digest",
        ):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must not be blank")
        if not self.gap_question_ids:
            raise ValueError("gap_question_ids must not be empty")
        if any(not value.strip() for value in self.gap_question_ids):
            raise ValueError("gap_question_ids must not contain blank values")
        if len(self.gap_question_ids) != len(set(self.gap_question_ids)):
            raise ValueError("gap_question_ids must not contain duplicates")
        if not isinstance(self.action, ResearchGapAction):
            raise TypeError("action must be a ResearchGapAction")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "report_brief_version": self.report_brief_version,
            "research_plan_digest": self.research_plan_digest,
            "gap_digest": self.gap_digest,
            "gap_question_ids": list(self.gap_question_ids),
            "action": self.action.value,
            "authorization_message_digest": self.authorization_message_digest,
        }

    def to_contract(
        self,
        *,
        work_id: str,
        tenant_id: str,
        contract_version: int,
        created_by: str,
        created_at: datetime,
    ) -> WorkContractSnapshot:
        return WorkContractSnapshot(
            work_id=work_id,
            tenant_id=tenant_id,
            contract_type=RESEARCH_GAP_DECISION_CONTRACT_TYPE,
            contract_version=contract_version,
            status=WorkContractStatus.PROVISIONAL,
            payload=self.to_payload(),
            created_by=created_by,
            created_at=created_at,
        )

    @classmethod
    def from_contract(cls, contract: WorkContractSnapshot) -> ResearchGapDecision:
        if contract.contract_type != RESEARCH_GAP_DECISION_CONTRACT_TYPE:
            raise ValueError("contract is not a report research gap decision")
        payload = dict(contract.payload)
        if payload.get("schema_version") != 1:
            raise ValueError("unsupported research gap decision schema_version")
        raw_gaps = payload.get("gap_question_ids")
        if not isinstance(raw_gaps, list):
            raise ValueError("gap_question_ids must be a list")
        return cls(
            report_brief_version=int(payload.get("report_brief_version", 0)),
            research_plan_digest=str(payload.get("research_plan_digest", "")),
            gap_digest=str(payload.get("gap_digest", "")),
            gap_question_ids=tuple(str(value) for value in raw_gaps),
            action=ResearchGapAction(str(payload.get("action", ""))),
            authorization_message_digest=str(payload.get("authorization_message_digest", "")),
        )


def explicitly_selects_gap_action(
    message: str,
    *,
    expected_version: int,
    expected_action: ResearchGapAction,
) -> bool:
    """Require one unambiguous, version-bound employee choice in the latest message."""

    clean = " ".join(message.split())
    if not clean or _NEGATION_RE.search(clean) or _QUESTION_RE.search(clean):
        return False
    versions = {int(value) for value in _REPORT_BRIEF_VERSION_RE.findall(clean)}
    if versions != {expected_version}:
        return False
    selected = {action for action, pattern in _ACTION_PATTERNS.items() if pattern.search(clean)}
    return selected == {expected_action}


def message_digest(message: str) -> str:
    clean = " ".join(message.split())
    return f"sha256:{sha256(clean.encode()).hexdigest()}"


def gap_digest(question_ids: tuple[str, ...]) -> str:
    encoded = "\n".join(sorted(question_ids)).encode()
    return f"sha256:{sha256(encoded).hexdigest()}"
