from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from agentseek_work import WorkContractSnapshot, WorkContractStatus

REPORT_BRIEF_CONTRACT_TYPE = "report-brief"
_DEFAULT_COVERAGE_PERIOD = "截至请求时间的最新可得数据"
_ALLOWED_OUTPUT_FORMATS = frozenset({"markdown", "docx", "pdf"})
_ALLOWED_CONFIDENTIALITY_LEVELS = frozenset({"public", "internal", "confidential", "restricted"})
_CONFIRM_INTENT_RE = re.compile(r"(?:我\s*)?(?:确认|同意|批准|认可)|\b(?:confirm|approve|accept)\b", re.IGNORECASE)
_NEGATED_CONFIRM_RE = re.compile(
    r"(?:不|未|尚未|暂不|不要|不能|无法|拒绝)\s*(?:确认|同意|批准|认可)"
    r"|\b(?:do\s+not|don't|not)\s+(?:confirm|approve|accept)\b",
    re.IGNORECASE,
)
_REQUEST_CONFIRM_RE = re.compile(r"请\s*(?:确认|同意|批准|认可)")
_QUESTION_CONFIRM_RE = re.compile(r"(?:是否|要不要|能否|可否).*(?:确认|同意|批准|认可)|(?:吗|么)[？?]?\s*$")
_REPORT_BRIEF_VERSION_PATTERNS = (
    re.compile(r"report\s*brief\s*(?:version|版本)?\s*[vV]?\s*(\d+)", re.IGNORECASE),
    re.compile(r"(?:报告简报|报告需求|报告需求简报)\s*(?:version|版本|第)?\s*[vV]?\s*(\d+)\s*版?"),
)

ReportOutputFormat = Literal["markdown", "docx", "pdf"]


class CoveragePeriodSource(StrEnum):
    EXPLICIT = "explicit"
    INFERRED = "inferred"
    PLAYBOOK_DEFAULT = "playbook_default"


class ResearchScope(StrEnum):
    SECURITIES_INDUSTRY = "securities_industry"
    SECURITIES_COMPANY = "securities_company"
    SECURITIES_BUSINESS_LINE = "securities_business_line"
    EXTERNAL_FACTOR_ON_SECURITIES = "external_factor_on_securities"


@dataclass(frozen=True, slots=True)
class ReportBrief:
    title: str
    research_scope: ResearchScope = ResearchScope.SECURITIES_INDUSTRY
    target_audience: tuple[str, ...] = ()
    coverage_period: str = _DEFAULT_COVERAGE_PERIOD
    coverage_period_source: CoveragePeriodSource = CoveragePeriodSource.PLAYBOOK_DEFAULT
    output_formats: tuple[str, ...] = ("docx",)
    delivery_sla_minutes: int = 50
    confidentiality_level: str = "internal"

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("title must not be blank")
        if not isinstance(self.research_scope, ResearchScope):
            raise TypeError("research_scope must be a ResearchScope")
        if not self.coverage_period.strip():
            raise ValueError("coverage_period must not be blank")
        if not isinstance(self.coverage_period_source, CoveragePeriodSource):
            raise TypeError("coverage_period_source must be a CoveragePeriodSource")
        if self.delivery_sla_minutes <= 0:
            raise ValueError("delivery_sla_minutes must be greater than zero")
        if not self.output_formats:
            raise ValueError("output_formats must not be empty")
        if any(value not in _ALLOWED_OUTPUT_FORMATS for value in self.output_formats):
            raise ValueError(
                "output_formats contains an unsupported format; "
                "supported formats are markdown, docx, and pdf"
            )
        if self.confidentiality_level not in _ALLOWED_CONFIDENTIALITY_LEVELS:
            raise ValueError("confidentiality_level is unsupported")
        _require_unique_nonblank(self.target_audience, "target_audience")
        _require_unique_nonblank(self.output_formats, "output_formats")

    @property
    def missing_fields(self) -> tuple[str, ...]:
        return () if self.target_audience else ("target_audience",)

    @property
    def is_confirmable(self) -> bool:
        return not self.missing_fields

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "title": self.title,
            "research_scope": self.research_scope.value,
            "target_audience": list(self.target_audience),
            "coverage_period": self.coverage_period,
            "coverage_period_source": self.coverage_period_source.value,
            "output_formats": list(self.output_formats),
            "delivery_sla_minutes": self.delivery_sla_minutes,
            "confidentiality_level": self.confidentiality_level,
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
            contract_type=REPORT_BRIEF_CONTRACT_TYPE,
            contract_version=contract_version,
            status=WorkContractStatus.PROVISIONAL,
            payload=self.to_payload(),
            created_by=created_by,
            created_at=created_at,
        )

    @classmethod
    def from_contract(cls, contract: WorkContractSnapshot) -> ReportBrief:
        if contract.contract_type != REPORT_BRIEF_CONTRACT_TYPE:
            raise ValueError("contract is not a report brief")
        payload = contract.payload
        schema_version = payload.get("schema_version")
        if schema_version not in {1, 2}:
            raise ValueError("unsupported report brief schema_version")
        return cls(
            title=_required_text(payload, "title"),
            research_scope=(
                ResearchScope.SECURITIES_INDUSTRY
                if schema_version == 1
                else ResearchScope(_required_text(payload, "research_scope"))
            ),
            target_audience=_text_tuple(payload, "target_audience"),
            coverage_period=_required_text(payload, "coverage_period"),
            coverage_period_source=CoveragePeriodSource(_required_text(payload, "coverage_period_source")),
            output_formats=_text_tuple(payload, "output_formats"),
            delivery_sla_minutes=_required_int(payload, "delivery_sla_minutes"),
            confidentiality_level=_required_text(payload, "confidentiality_level"),
        )


def validate_report_brief_scope(
    brief: ReportBrief,
    *,
    allowed_scopes: Sequence[str],
    topic_anchor_terms: Sequence[str],
) -> None:
    """Fail closed when a ReportBrief falls outside the declared Playbook role."""

    allowed = {str(value).strip() for value in allowed_scopes if str(value).strip()}
    if brief.research_scope.value not in allowed:
        raise ValueError(
            f"研究范围 {brief.research_scope.value} 不在当前证券研究 Playbook 的允许范围内。"
        )
    title = brief.title.casefold()
    anchors = tuple(str(value).strip().casefold() for value in topic_anchor_terms if str(value).strip())
    if not anchors or not any(anchor in title for anchor in anchors):
        raise ValueError(
            "SCOPE_MISMATCH：当前数字员工仅承担证券行业、证券公司、证券业务线，"
            "以及外部因素对证券行业影响的研究。请在报告主题中明确证券研究对象或影响关系。"
        )


def explicitly_confirms_report_brief(message: str, *, expected_version: int) -> bool:
    """Return whether one employee message explicitly confirms exactly one Brief version."""

    text = str(message or "").strip()
    if expected_version <= 0 or not text:
        return False
    if not _CONFIRM_INTENT_RE.search(text):
        return False
    if _NEGATED_CONFIRM_RE.search(text) or _REQUEST_CONFIRM_RE.search(text) or _QUESTION_CONFIRM_RE.search(text):
        return False
    versions = {
        int(match.group(1))
        for pattern in _REPORT_BRIEF_VERSION_PATTERNS
        for match in pattern.finditer(text)
    }
    return versions == {expected_version}


def _require_unique_nonblank(values: tuple[str, ...], field_name: str) -> None:
    if any(not value.strip() for value in values):
        raise ValueError(f"{field_name} must not contain blank values")
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicates")


def _required_text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"report brief {key} must be non-blank text")
    return value


def _required_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"report brief {key} must be an integer")
    return value


def _text_tuple(payload: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"report brief {key} must be a list of text values")
    return tuple(value)
