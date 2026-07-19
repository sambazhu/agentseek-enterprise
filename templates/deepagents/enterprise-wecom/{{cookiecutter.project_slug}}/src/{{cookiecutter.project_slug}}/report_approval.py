from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any

from agentseek_work import WorkContractSnapshot, WorkContractStatus

REPORT_APPROVAL_CONTRACT_TYPE = "report-approval"
REPORT_APPROVAL_SCHEMA_VERSION = 1
REPORT_APPROVAL_POLICY_ID = "industry-report-v1"
_SHA256_RE = re.compile(r"^sha256:[a-f0-9]{64}$")

_DRAFT_VERSION_PATTERNS = (
    re.compile(r"report\s*draft\s*(?:version|版本)?\s*[vV]?\s*(\d+)", re.IGNORECASE),
    re.compile(r"(?:报告初稿|初稿|报告草稿)\s*(?:version|版本|第)?\s*[vV]?\s*(\d+)\s*版?"),
)
_REQUEST_APPROVAL_RE = re.compile(
    r"(?:提交|申请|发起).{0,16}(?:审批|批准)|送审|"
    r"\b(?:submit|request)\b.{0,24}\bapproval\b",
    re.IGNORECASE,
)
_APPROVE_RE = re.compile(
    r"(?:审批通过|批准|同意批准)|\bapprove(?:d)?\b",
    re.IGNORECASE,
)
_NEGATED_APPROVAL_RE = re.compile(
    r"(?:不|未|尚未|暂不|不要|不能|无法|拒绝).{0,8}(?:提交|申请|发起|送审|审批|批准)|"
    r"\b(?:do\s+not|don't|not)\b.{0,16}\b(?:submit|request|approve|approval)\b",
    re.IGNORECASE,
)
_REQUEST_ACTOR_RE = re.compile(r"请\s*(?:审批通过|批准|同意批准)")
_QUESTION_RE = re.compile(r"(?:是否|要不要|能否|可否).*(?:审批|批准)|(?:吗|么)[？?]?\s*$")


@dataclass(frozen=True, slots=True)
class ReportApproval:
    """A versioned approval request bound to one immutable ReportDraft payload."""

    report_draft_version: int
    report_draft_digest: str
    request_message_digest: str
    policy_id: str = REPORT_APPROVAL_POLICY_ID

    def __post_init__(self) -> None:
        if self.report_draft_version <= 0:
            raise ValueError("report_draft_version must be greater than zero")
        for field_name in ("report_draft_digest", "request_message_digest", "policy_id"):
            value = getattr(self, field_name)
            if not value.strip():
                raise ValueError(f"{field_name} must not be blank")
        if not _SHA256_RE.fullmatch(self.report_draft_digest):
            raise ValueError("report_draft_digest must be a canonical sha256 digest")
        if not _SHA256_RE.fullmatch(self.request_message_digest):
            raise ValueError("request_message_digest must be a canonical sha256 digest")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": REPORT_APPROVAL_SCHEMA_VERSION,
            "report_draft_version": self.report_draft_version,
            "report_draft_digest": self.report_draft_digest,
            "request_message_digest": self.request_message_digest,
            "policy_id": self.policy_id,
            "approval_scope": "report_content",
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
            contract_type=REPORT_APPROVAL_CONTRACT_TYPE,
            contract_version=contract_version,
            status=WorkContractStatus.PROVISIONAL,
            payload=self.to_payload(),
            created_by=created_by,
            created_at=created_at,
        )

    @classmethod
    def from_contract(cls, contract: WorkContractSnapshot) -> ReportApproval:
        if contract.contract_type != REPORT_APPROVAL_CONTRACT_TYPE:
            raise ValueError("contract is not a report approval")
        payload = dict(contract.payload)
        if payload.get("schema_version") != REPORT_APPROVAL_SCHEMA_VERSION:
            raise ValueError("unsupported report approval schema_version")
        if payload.get("approval_scope") != "report_content":
            raise ValueError("unsupported report approval scope")
        return cls(
            report_draft_version=int(payload.get("report_draft_version", 0)),
            report_draft_digest=str(payload.get("report_draft_digest", "")),
            request_message_digest=str(payload.get("request_message_digest", "")),
            policy_id=str(payload.get("policy_id", "")),
        )


def explicitly_requests_report_approval(message: str, *, expected_version: int) -> bool:
    """Require one explicit, exact-version request to enter the approval queue."""

    text = " ".join(str(message or "").split())
    return bool(
        expected_version > 0
        and text
        and _REQUEST_APPROVAL_RE.search(text)
        and not _NEGATED_APPROVAL_RE.search(text)
        and not _QUESTION_RE.search(text)
        and _versions(text) == {expected_version}
    )


def explicitly_approves_report_draft(message: str, *, expected_version: int) -> bool:
    """Require one explicit approver decision for exactly one ReportDraft version."""

    text = " ".join(str(message or "").split())
    return bool(
        expected_version > 0
        and text
        and _APPROVE_RE.search(text)
        and not _REQUEST_APPROVAL_RE.search(text)
        and not _REQUEST_ACTOR_RE.search(text)
        and not _NEGATED_APPROVAL_RE.search(text)
        and not _QUESTION_RE.search(text)
        and _versions(text) == {expected_version}
    )


def approval_state(contract: WorkContractSnapshot) -> str:
    if contract.status is WorkContractStatus.CONFIRMED:
        return "approved"
    if contract.status is WorkContractStatus.PROVISIONAL:
        return "pending"
    return "superseded"


def approval_message_digest(message: str) -> str:
    clean = " ".join(str(message or "").split())
    return f"sha256:{sha256(clean.encode()).hexdigest()}"


def _versions(message: str) -> set[int]:
    return {
        int(match.group(1))
        for pattern in _DRAFT_VERSION_PATTERNS
        for match in pattern.finditer(message)
    }
