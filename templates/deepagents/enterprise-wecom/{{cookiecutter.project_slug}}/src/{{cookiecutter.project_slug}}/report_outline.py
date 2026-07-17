from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any

from agentseek_work import SourceRecord, WorkContractSnapshot, WorkContractStatus

REPORT_OUTLINE_CONTRACT_TYPE = "report-outline"

_CONFIRM_INTENT_RE = re.compile(
    r"(?:我\s*)?(?:确认|同意|批准|认可)|\b(?:confirm|approve|accept)\b",
    re.IGNORECASE,
)
_NEGATED_CONFIRM_RE = re.compile(
    r"(?:不|未|尚未|暂不|不要|不能|无法|拒绝)\s*(?:确认|同意|批准|认可)"
    r"|\b(?:do\s+not|don't|not)\s+(?:confirm|approve|accept)\b",
    re.IGNORECASE,
)
_REQUEST_CONFIRM_RE = re.compile(r"请\s*(?:确认|同意|批准|认可)")
_QUESTION_CONFIRM_RE = re.compile(r"(?:是否|要不要|能否|可否).*(?:确认|同意|批准|认可)|(?:吗|么)[？?]?\s*$")
_OUTLINE_VERSION_PATTERNS = (
    re.compile(r"report\s*outline\s*(?:version|版本)?\s*[vV]?\s*(\d+)", re.IGNORECASE),
    re.compile(r"(?:报告提纲|报告大纲|提纲)\s*(?:version|版本|第)?\s*[vV]?\s*(\d+)\s*版?"),
)


class OutlineEvidenceStatus(StrEnum):
    SUPPORTED = "supported"
    UNRESOLVED = "unresolved"


class OutlineSectionStatus(StrEnum):
    SUPPORTED = "supported"
    PARTIAL = "partial"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class OutlineQuestion:
    question_id: str
    prompt: str
    source_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.question_id, "question_id")
        _require_text(self.prompt, "prompt")
        _require_unique_nonblank(self.source_ids, "source_ids")

    @property
    def evidence_status(self) -> OutlineEvidenceStatus:
        return OutlineEvidenceStatus.SUPPORTED if self.source_ids else OutlineEvidenceStatus.UNRESOLVED

    def to_payload(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "prompt": self.prompt,
            "evidence_status": self.evidence_status.value,
            "source_ids": list(self.source_ids),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> OutlineQuestion:
        question = cls(
            question_id=_required_text(payload, "question_id"),
            prompt=_required_text(payload, "prompt"),
            source_ids=_text_tuple(payload, "source_ids"),
        )
        if payload.get("evidence_status") != question.evidence_status.value:
            raise ValueError("report outline question evidence_status is inconsistent")
        return question


@dataclass(frozen=True, slots=True)
class OutlineSection:
    section_id: str
    title: str
    questions: tuple[OutlineQuestion, ...]

    def __post_init__(self) -> None:
        _require_text(self.section_id, "section_id")
        _require_text(self.title, "title")
        if not self.questions:
            raise ValueError("outline section requires at least one applicable question")
        _require_unique((question.question_id for question in self.questions), "question_id")

    @property
    def status(self) -> OutlineSectionStatus:
        supported = sum(question.evidence_status is OutlineEvidenceStatus.SUPPORTED for question in self.questions)
        if supported == len(self.questions):
            return OutlineSectionStatus.SUPPORTED
        if supported:
            return OutlineSectionStatus.PARTIAL
        return OutlineSectionStatus.UNRESOLVED

    @property
    def source_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(source_id for question in self.questions for source_id in question.source_ids))

    @property
    def unresolved_question_ids(self) -> tuple[str, ...]:
        return tuple(
            question.question_id
            for question in self.questions
            if question.evidence_status is OutlineEvidenceStatus.UNRESOLVED
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "section_id": self.section_id,
            "title": self.title,
            "status": self.status.value,
            "questions": [question.to_payload() for question in self.questions],
            "source_ids": list(self.source_ids),
            "unresolved_question_ids": list(self.unresolved_question_ids),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> OutlineSection:
        raw_questions = payload.get("questions")
        if not isinstance(raw_questions, list):
            raise ValueError("report outline section questions must be a list")
        questions = tuple(
            OutlineQuestion.from_payload(_mapping(value, "report outline question"))
            for value in raw_questions
        )
        section = cls(
            section_id=_required_text(payload, "section_id"),
            title=_required_text(payload, "title"),
            questions=questions,
        )
        if payload.get("status") != section.status.value:
            raise ValueError("report outline section status is inconsistent")
        if _text_tuple(payload, "source_ids") != section.source_ids:
            raise ValueError("report outline section source_ids are inconsistent")
        if _text_tuple(payload, "unresolved_question_ids") != section.unresolved_question_ids:
            raise ValueError("report outline section unresolved questions are inconsistent")
        return section


@dataclass(frozen=True, slots=True)
class ReportOutline:
    report_brief_version: int
    research_plan_digest: str
    research_scope: str
    report_title: str
    template_id: str
    template_version: str
    source_set_digest: str
    sections: tuple[OutlineSection, ...]
    gap_decision_contract_version: int | None = None

    def __post_init__(self) -> None:
        if self.report_brief_version <= 0:
            raise ValueError("report_brief_version must be greater than zero")
        for field_name in (
            "research_plan_digest",
            "research_scope",
            "report_title",
            "template_id",
            "template_version",
            "source_set_digest",
        ):
            _require_text(getattr(self, field_name), field_name)
        if not self.source_set_digest.startswith("sha256:"):
            raise ValueError("source_set_digest must use sha256")
        if not self.sections:
            raise ValueError("report outline requires at least one applicable section")
        _require_unique((section.section_id for section in self.sections), "section_id")
        _require_unique(
            (question.question_id for section in self.sections for question in section.questions),
            "question_id",
        )
        if self.gap_decision_contract_version is not None and self.gap_decision_contract_version <= 0:
            raise ValueError("gap_decision_contract_version must be greater than zero")

    @property
    def source_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(source_id for section in self.sections for source_id in section.source_ids))

    @property
    def unresolved_question_ids(self) -> tuple[str, ...]:
        return tuple(
            question_id
            for section in self.sections
            for question_id in section.unresolved_question_ids
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "report_brief_version": self.report_brief_version,
            "research_plan_digest": self.research_plan_digest,
            "research_scope": self.research_scope,
            "report_title": self.report_title,
            "template_id": self.template_id,
            "template_version": self.template_version,
            "source_set_digest": self.source_set_digest,
            "source_ids": list(self.source_ids),
            "unresolved_question_ids": list(self.unresolved_question_ids),
            "gap_decision_contract_version": self.gap_decision_contract_version,
            "sections": [section.to_payload() for section in self.sections],
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
            contract_type=REPORT_OUTLINE_CONTRACT_TYPE,
            contract_version=contract_version,
            status=WorkContractStatus.PROVISIONAL,
            payload=self.to_payload(),
            created_by=created_by,
            created_at=created_at,
        )

    @classmethod
    def from_contract(cls, contract: WorkContractSnapshot) -> ReportOutline:
        if contract.contract_type != REPORT_OUTLINE_CONTRACT_TYPE:
            raise ValueError("contract is not a report outline")
        payload = dict(contract.payload)
        if payload.get("schema_version") != 1:
            raise ValueError("unsupported report outline schema_version")
        raw_sections = payload.get("sections")
        if not isinstance(raw_sections, list):
            raise ValueError("report outline sections must be a list")
        gap_version = payload.get("gap_decision_contract_version")
        outline = cls(
            report_brief_version=_required_int(payload, "report_brief_version"),
            research_plan_digest=_required_text(payload, "research_plan_digest"),
            research_scope=_required_text(payload, "research_scope"),
            report_title=_required_text(payload, "report_title"),
            template_id=_required_text(payload, "template_id"),
            template_version=_required_text(payload, "template_version"),
            source_set_digest=_required_text(payload, "source_set_digest"),
            sections=tuple(
                OutlineSection.from_payload(_mapping(value, "report outline section"))
                for value in raw_sections
            ),
            gap_decision_contract_version=(
                None
                if gap_version is None
                else _required_int(payload, "gap_decision_contract_version")
            ),
        )
        if _text_tuple(payload, "source_ids") != outline.source_ids:
            raise ValueError("report outline source_ids are inconsistent")
        if _text_tuple(payload, "unresolved_question_ids") != outline.unresolved_question_ids:
            raise ValueError("report outline unresolved questions are inconsistent")
        return outline


def source_set_digest(sources: Sequence[SourceRecord]) -> str:
    encoded = "\n".join(
        f"{source.source_id}:{source.result_digest}"
        for source in sorted(sources, key=lambda value: value.source_id)
    ).encode()
    return f"sha256:{sha256(encoded).hexdigest()}"


def explicitly_confirms_report_outline(message: str, *, expected_version: int) -> bool:
    text = str(message or "").strip()
    if expected_version <= 0 or not text or not _CONFIRM_INTENT_RE.search(text):
        return False
    if _NEGATED_CONFIRM_RE.search(text) or _REQUEST_CONFIRM_RE.search(text) or _QUESTION_CONFIRM_RE.search(text):
        return False
    versions = {
        int(match.group(1))
        for pattern in _OUTLINE_VERSION_PATTERNS
        for match in pattern.finditer(text)
    }
    return versions == {expected_version}


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    return {str(key): item for key, item in value.items()}


def _required_text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"report outline {key} must be non-blank text")
    return value


def _required_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"report outline {key} must be an integer")
    return value


def _text_tuple(payload: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"report outline {key} must be a list of text values")
    return tuple(value)


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")


def _require_unique_nonblank(values: tuple[str, ...], field_name: str) -> None:
    if any(not value.strip() for value in values):
        raise ValueError(f"{field_name} must not contain blank values")
    _require_unique(values, field_name)


def _require_unique(values: Iterable[str], field_name: str) -> None:
    materialized = tuple(values)
    if len(materialized) != len(set(materialized)):
        raise ValueError(f"{field_name} must not contain duplicates")
