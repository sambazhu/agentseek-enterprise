from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import TYPE_CHECKING, Any

from agentseek_work import (
    ClaimRecord,
    ClaimReviewerStatus,
    ClaimType,
    ClaimVerificationStatus,
    EvidenceRecord,
    SourceRecord,
    SourceType,
    WorkContractSnapshot,
    WorkContractStatus,
    WorkNotFoundError,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator

from {{ cookiecutter.project_slug }}.report_outline import ReportOutline

if TYPE_CHECKING:
    from {{ cookiecutter.project_slug }}.work_composition import IndustryReportWorkComposition

REPORT_DRAFT_CONTRACT_TYPE = "report-draft"
REPORT_DRAFT_SCHEMA_VERSION = 1
REPORT_DRAFT_MARKDOWN_BEGIN = "[REPORT_DRAFT_MARKDOWN]"
REPORT_DRAFT_MARKDOWN_END = "[/REPORT_DRAFT_MARKDOWN]"
MAX_EVIDENCE_EXCERPT_CHARS = 1800
MAX_DRAFT_CLAIMS = 60

MCPInvoker = Callable[[str, str, dict[str, Any], bool], Awaitable[str]]

_FORBIDDEN_CONTENT_RE = re.compile(
    r"(?:/(?:Users|home|private|var)/\S+|(?:password|token|secret|api[_-]?key)\s*[:=]\s*\S+|\.env(?:\b|/))",
    re.IGNORECASE,
)


class DraftQualityStatus(StrEnum):
    PASS = "pass"  # noqa: S105 - quality status, not a credential
    WARNING = "warning"
    BLOCKED = "blocked"


class DraftClaimProposal(BaseModel):
    """One model-authored draft assertion submitted to the deterministic ledger gate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    section_id: str = Field(min_length=1, max_length=256)
    statement: str = Field(min_length=1, max_length=1600)
    claim_type: ClaimType
    evidence_ids: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("section_id", "statement")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        clean = " ".join(value.split())
        if not clean:
            raise ValueError("draft claim text must not be blank")
        return clean

    @field_validator("evidence_ids")
    @classmethod
    def _validate_evidence_ids(cls, values: list[str]) -> list[str]:
        clean = [value.strip() for value in values]
        if any(not value for value in clean):
            raise ValueError("evidence_ids must not contain blank values")
        if len(clean) != len(set(clean)):
            raise ValueError("evidence_ids must not contain duplicates")
        return clean


@dataclass(frozen=True, slots=True)
class DraftQualityCheck:
    check_id: str
    status: DraftQualityStatus
    message: str

    def __post_init__(self) -> None:
        _require_text(self.check_id, "check_id")
        _require_text(self.message, "message")
        if not isinstance(self.status, DraftQualityStatus):
            raise TypeError("status must be a DraftQualityStatus")

    def as_dict(self) -> dict[str, str]:
        return {"check_id": self.check_id, "status": self.status.value, "message": self.message}


@dataclass(frozen=True, slots=True)
class DraftSection:
    section_id: str
    title: str
    claim_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text(self.section_id, "section_id")
        _require_text(self.title, "title")
        _require_unique_nonblank(self.claim_ids, "claim_ids")

    def as_dict(self) -> dict[str, object]:
        return {"section_id": self.section_id, "title": self.title, "claim_ids": list(self.claim_ids)}


@dataclass(frozen=True, slots=True)
class ReportDraft:
    report_outline_version: int
    report_brief_version: int
    report_title: str
    source_set_digest: str
    evidence_set_digest: str
    claim_set_digest: str
    sections: tuple[DraftSection, ...]
    unresolved_question_ids: tuple[str, ...]
    markdown: str
    quality_checks: tuple[DraftQualityCheck, ...]

    def __post_init__(self) -> None:
        if self.report_outline_version <= 0 or self.report_brief_version <= 0:
            raise ValueError("report draft versions must be greater than zero")
        for field_name in (
            "report_title",
            "source_set_digest",
            "evidence_set_digest",
            "claim_set_digest",
            "markdown",
        ):
            _require_text(getattr(self, field_name), field_name)
        if not self.sections:
            raise ValueError("report draft requires at least one section")
        _require_unique((section.section_id for section in self.sections), "section IDs")
        _require_unique_nonblank(self.unresolved_question_ids, "unresolved_question_ids", allow_empty=True)
        if not self.quality_checks:
            raise ValueError("report draft requires quality checks")
        claim_ids = tuple(claim_id for section in self.sections for claim_id in section.claim_ids)
        _require_unique(claim_ids, "draft claim IDs")

    @property
    def quality_status(self) -> DraftQualityStatus:
        statuses = {check.status for check in self.quality_checks}
        if DraftQualityStatus.BLOCKED in statuses:
            return DraftQualityStatus.BLOCKED
        if DraftQualityStatus.WARNING in statuses:
            return DraftQualityStatus.WARNING
        return DraftQualityStatus.PASS

    @property
    def claim_ids(self) -> tuple[str, ...]:
        return tuple(claim_id for section in self.sections for claim_id in section.claim_ids)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": REPORT_DRAFT_SCHEMA_VERSION,
            "report_outline_version": self.report_outline_version,
            "report_brief_version": self.report_brief_version,
            "report_title": self.report_title,
            "source_set_digest": self.source_set_digest,
            "evidence_set_digest": self.evidence_set_digest,
            "claim_set_digest": self.claim_set_digest,
            "claim_ids": list(self.claim_ids),
            "unresolved_question_ids": list(self.unresolved_question_ids),
            "quality_status": self.quality_status.value,
            "quality_checks": [check.as_dict() for check in self.quality_checks],
            "sections": [section.as_dict() for section in self.sections],
            "markdown": self.markdown,
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
            contract_type=REPORT_DRAFT_CONTRACT_TYPE,
            contract_version=contract_version,
            status=WorkContractStatus.PROVISIONAL,
            payload=self.to_payload(),
            created_by=created_by,
            created_at=created_at,
        )

    @classmethod
    def from_contract(cls, contract: WorkContractSnapshot) -> ReportDraft:
        if contract.contract_type != REPORT_DRAFT_CONTRACT_TYPE:
            raise ValueError("contract is not a report draft")
        payload = dict(contract.payload)
        if payload.get("schema_version") != REPORT_DRAFT_SCHEMA_VERSION:
            raise ValueError("unsupported report draft schema_version")
        sections = tuple(
            DraftSection(
                section_id=_required_text(item, "section_id"),
                title=_required_text(item, "title"),
                claim_ids=_text_tuple(item, "claim_ids"),
            )
            for item in _mapping_sequence(payload.get("sections"), "sections")
        )
        checks = tuple(
            DraftQualityCheck(
                check_id=_required_text(item, "check_id"),
                status=DraftQualityStatus(_required_text(item, "status")),
                message=_required_text(item, "message"),
            )
            for item in _mapping_sequence(payload.get("quality_checks"), "quality_checks")
        )
        draft = cls(
            report_outline_version=_required_int(payload, "report_outline_version"),
            report_brief_version=_required_int(payload, "report_brief_version"),
            report_title=_required_text(payload, "report_title"),
            source_set_digest=_required_text(payload, "source_set_digest"),
            evidence_set_digest=_required_text(payload, "evidence_set_digest"),
            claim_set_digest=_required_text(payload, "claim_set_digest"),
            sections=sections,
            unresolved_question_ids=_text_tuple(payload, "unresolved_question_ids"),
            markdown=_required_text(payload, "markdown"),
            quality_checks=checks,
        )
        if _text_tuple(payload, "claim_ids") != draft.claim_ids:
            raise ValueError("report draft claim_ids are inconsistent")
        if payload.get("quality_status") != draft.quality_status.value:
            raise ValueError("report draft quality_status is inconsistent")
        return draft


@dataclass(frozen=True, slots=True)
class DraftContextResult:
    work_id: str
    report_outline_version: int
    report_brief_version: int
    report_title: str
    evidence: tuple[EvidenceRecord, ...]
    unavailable_source_ids: tuple[str, ...]
    sections: tuple[dict[str, object], ...]

    def as_dict(self) -> dict[str, object]:
        evidence_by_id = {item.evidence_id: item for item in self.evidence}
        return {
            "work_id": self.work_id,
            "report_outline_version": self.report_outline_version,
            "report_brief_version": self.report_brief_version,
            "report_title": self.report_title,
            "evidence": [
                {
                    "evidence_id": item.evidence_id,
                    "source_id": item.source_id,
                    "locator": item.locator,
                    "excerpt": item.excerpt,
                    "confidence": item.confidence,
                    "section_ids": list(_metadata_texts(item.metadata, "section_ids")),
                    "question_ids": list(_metadata_texts(item.metadata, "question_ids")),
                }
                for item in self.evidence
            ],
            "sections": [
                {
                    **section,
                    "evidence_ids": [
                        evidence_id
                        for evidence_id in _section_evidence_ids(section)
                        if evidence_id in evidence_by_id
                    ],
                }
                for section in self.sections
            ],
            "unavailable_source_ids": list(self.unavailable_source_ids),
            "instructions": (
                "仅基于 evidence 中的 excerpt 起草；事实和推断必须绑定 evidence_ids。"
                "未解决问题写成风险或待确认项，不得用模型常识补齐。"
            ),
        }


async def prepare_report_draft_context(
    *,
    composition: IndustryReportWorkComposition,
    state: Mapping[str, object],
    runtime_context: object | None,
    invoke_mcp: MCPInvoker,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> DraftContextResult:
    item, outline_contract, outline = composition.current_confirmed_report_outline(state, runtime_context)
    sources = _outline_sources(composition, item.tenant_id, item.work_id, outline)
    internal_sources = tuple(
        source
        for source in sources
        if source.source_type is SourceType.DEPARTMENT_KNOWLEDGE
        and "citation" in source.allowed_uses
        and isinstance(source.metadata.get("chunk_id"), str)
    )
    chunk_ids = tuple(dict.fromkeys(str(source.metadata["chunk_id"]) for source in internal_sources))
    chunks = await _read_chunks(chunk_ids, invoke_mcp) if chunk_ids else {}
    evidence: list[EvidenceRecord] = []
    for source in internal_sources:
        chunk_id = str(source.metadata["chunk_id"])
        content = chunks.get(chunk_id)
        if content is None:
            continue
        if _digest_text(content) != source.content_hash:
            raise RuntimeError(f"source content changed after registration: {source.source_id}")
        evidence_id = _evidence_id(source, outline_contract.contract_version)
        try:
            record = composition.repository.get_evidence_record(
                tenant_id=item.tenant_id,
                evidence_id=evidence_id,
            )
        except WorkNotFoundError:
            excerpt = content[:MAX_EVIDENCE_EXCERPT_CHARS].strip()
            record = composition.repository.put_evidence_record(EvidenceRecord(
                evidence_id=evidence_id,
                work_id=item.work_id,
                tenant_id=item.tenant_id,
                source_id=source.source_id,
                locator=source.locator or f"source://{source.source_id}",
                excerpt=excerpt,
                confidence=0.95,
                extraction_method="department_knowledge_chunk",
                created_at=clock(),
                metadata={
                    "report_outline_version": outline_contract.contract_version,
                    "report_brief_version": outline.report_brief_version,
                    "source_result_digest": source.result_digest,
                    "section_ids": list(_metadata_texts(source.metadata, "section_ids")),
                    "question_ids": list(_metadata_texts(source.metadata, "question_ids")),
                    "truncated": len(content) > len(excerpt),
                },
            ))
        evidence.append(record)
    evidence_by_source = {record.source_id: record.evidence_id for record in evidence}
    sections: tuple[dict[str, object], ...] = tuple(
        {
            "section_id": section.section_id,
            "title": section.title,
            "question_ids": [question.question_id for question in section.questions],
            "unresolved_question_ids": list(section.unresolved_question_ids),
            "evidence_ids": [
                evidence_by_source[source_id]
                for source_id in section.source_ids
                if source_id in evidence_by_source
            ],
        }
        for section in outline.sections
    )
    unavailable = tuple(source.source_id for source in sources if source.source_id not in evidence_by_source)
    return DraftContextResult(
        work_id=item.work_id,
        report_outline_version=outline_contract.contract_version,
        report_brief_version=outline.report_brief_version,
        report_title=outline.report_title,
        evidence=tuple(evidence),
        unavailable_source_ids=unavailable,
        sections=sections,
    )


def build_report_draft(  # noqa: C901 - validates the complete draft ledger boundary
    *,
    composition: IndustryReportWorkComposition,
    state: Mapping[str, object],
    runtime_context: object | None,
    proposals: Sequence[DraftClaimProposal],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ReportDraft:
    if not proposals:
        raise ValueError("报告初稿至少需要一条结构化 Claim。")
    if len(proposals) > MAX_DRAFT_CLAIMS:
        raise ValueError(f"报告初稿最多允许 {MAX_DRAFT_CLAIMS} 条 Claim。")
    item, outline_contract, outline = composition.current_confirmed_report_outline(state, runtime_context)
    evidence = _current_outline_evidence(
        composition,
        tenant_id=item.tenant_id,
        work_id=item.work_id,
        outline_version=outline_contract.contract_version,
        outline=outline,
    )
    evidence_by_id = {record.evidence_id: record for record in evidence}
    outline_sections = {section.section_id: section for section in outline.sections}
    proposal_sections = {proposal.section_id for proposal in proposals}
    missing_sections = tuple(section_id for section_id in outline_sections if section_id not in proposal_sections)
    if missing_sections:
        raise ValueError("报告初稿缺少提纲章节：" + "、".join(missing_sections))

    validated: list[tuple[DraftClaimProposal, tuple[str, ...], str]] = []
    for proposal in proposals:
        section = outline_sections.get(proposal.section_id)
        if section is None:
            raise ValueError(f"Claim 引用了未确认提纲中的章节：{proposal.section_id}")
        if _FORBIDDEN_CONTENT_RE.search(proposal.statement):
            raise ValueError("Claim 包含宿主路径或凭据样式内容，质量门拒绝保存。")
        evidence_ids = tuple(proposal.evidence_ids)
        if proposal.claim_type in {ClaimType.FACT, ClaimType.INFERENCE} and not evidence_ids:
            raise ValueError("事实和推断 Claim 必须绑定 EvidenceRecord。")
        for evidence_id in evidence_ids:
            record = evidence_by_id.get(evidence_id)
            if record is None:
                raise ValueError(f"Claim 引用了当前提纲之外的 EvidenceRecord：{evidence_id}")
            if record.source_id not in section.source_ids:
                raise ValueError(f"EvidenceRecord {evidence_id} 未绑定章节 {section.section_id}")
        claim_id = _claim_id(
            item.work_id,
            outline_contract.contract_version,
            proposal.section_id,
            proposal.statement,
            proposal.claim_type,
            evidence_ids,
        )
        validated.append((proposal, evidence_ids, claim_id))
    claim_ids = tuple(claim_id for _proposal, _evidence_ids, claim_id in validated)
    if len(set(claim_ids)) != len(claim_ids):
        raise ValueError("报告初稿不能重复提交相同 Claim。")

    claims: list[ClaimRecord] = []
    for proposal, evidence_ids, claim_id in validated:
        try:
            claim = composition.repository.get_claim_record(tenant_id=item.tenant_id, claim_id=claim_id)
        except WorkNotFoundError:
            claim = composition.repository.put_claim_record(ClaimRecord(
                claim_id=claim_id,
                work_id=item.work_id,
                tenant_id=item.tenant_id,
                section_id=proposal.section_id,
                statement=proposal.statement,
                claim_type=proposal.claim_type,
                evidence_ids=evidence_ids,
                verification_status=ClaimVerificationStatus.UNVERIFIED,
                reviewer_status=ClaimReviewerStatus.PENDING,
                created_at=clock(),
                metadata={
                    "report_outline_version": outline_contract.contract_version,
                    "report_brief_version": outline.report_brief_version,
                    "source_set_digest": outline.source_set_digest,
                },
            ))
        claims.append(claim)

    evidence_digest = evidence_set_digest(evidence)
    claims_digest = claim_set_digest(claims)
    checks = _quality_checks(outline, claims, evidence)
    markdown = _render_markdown(outline, claims, evidence)
    sections = tuple(
        DraftSection(
            section_id=section.section_id,
            title=section.title,
            claim_ids=tuple(claim.claim_id for claim in claims if claim.section_id == section.section_id),
        )
        for section in outline.sections
    )
    return ReportDraft(
        report_outline_version=outline_contract.contract_version,
        report_brief_version=outline.report_brief_version,
        report_title=outline.report_title,
        source_set_digest=outline.source_set_digest,
        evidence_set_digest=evidence_digest,
        claim_set_digest=claims_digest,
        sections=sections,
        unresolved_question_ids=outline.unresolved_question_ids,
        markdown=markdown,
        quality_checks=checks,
    )


def evidence_set_digest(evidence: Sequence[EvidenceRecord]) -> str:
    return _digest([f"{item.evidence_id}:{item.source_id}" for item in sorted(evidence, key=lambda x: x.evidence_id)])


def claim_set_digest(claims: Sequence[ClaimRecord]) -> str:
    return _digest([f"{item.claim_id}:{','.join(item.evidence_ids)}" for item in sorted(claims, key=lambda x: x.claim_id)])


async def _read_chunks(chunk_ids: Sequence[str], invoke_mcp: MCPInvoker) -> dict[str, str]:
    raw = await invoke_mcp(
        "department-knowledge",
        "knowledge_read_chunks",
        {"chunk_ids": list(chunk_ids)},
        False,
    )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("knowledge_read_chunks returned non-JSON content") from exc
    if not isinstance(payload, Mapping):
        raise RuntimeError("knowledge_read_chunks returned an invalid payload")
    chunks = payload.get("chunks")
    if not isinstance(chunks, Sequence) or isinstance(chunks, (str, bytes)):
        raise RuntimeError("knowledge_read_chunks returned an invalid chunks list")
    result: dict[str, str] = {}
    for raw_chunk in chunks:
        if not isinstance(raw_chunk, Mapping):
            continue
        chunk_id = str(raw_chunk.get("chunk_id") or "").strip()
        content = str(raw_chunk.get("content") or "").strip()
        if chunk_id and content:
            result[chunk_id] = content
    return result


def _outline_sources(
    composition: IndustryReportWorkComposition,
    tenant_id: str,
    work_id: str,
    outline: ReportOutline,
) -> tuple[SourceRecord, ...]:
    by_id = {
        source.source_id: source
        for source in composition.repository.list_source_records(tenant_id=tenant_id, work_id=work_id)
    }
    missing = tuple(source_id for source_id in outline.source_ids if source_id not in by_id)
    if missing:
        raise RuntimeError("ReportOutline 引用的 SourceRecord 已不可用：" + "、".join(missing))
    return tuple(by_id[source_id] for source_id in outline.source_ids)


def _current_outline_evidence(
    composition: IndustryReportWorkComposition,
    *,
    tenant_id: str,
    work_id: str,
    outline_version: int,
    outline: ReportOutline,
) -> tuple[EvidenceRecord, ...]:
    source_ids = set(outline.source_ids)
    return tuple(
        record
        for record in composition.repository.list_evidence_records(tenant_id=tenant_id, work_id=work_id)
        if record.source_id in source_ids
        and record.metadata.get("report_outline_version") == outline_version
    )


def _quality_checks(
    outline: ReportOutline,
    claims: Sequence[ClaimRecord],
    evidence: Sequence[EvidenceRecord],
) -> tuple[DraftQualityCheck, ...]:
    cited = {evidence_id for claim in claims for evidence_id in claim.evidence_ids}
    checks = [
        DraftQualityCheck("outline_binding", DraftQualityStatus.PASS, "初稿精确绑定已确认 ReportOutline。"),
        DraftQualityCheck("claim_evidence_binding", DraftQualityStatus.PASS, "事实和推断 Claim 均绑定 Evidence。"),
        DraftQualityCheck("citation_locator", DraftQualityStatus.PASS, "所有已引用 Evidence 均包含稳定 locator。"),
        DraftQualityCheck("sensitive_content", DraftQualityStatus.PASS, "未检测到宿主路径或凭据样式内容。"),
        DraftQualityCheck(
            "semantic_verification",
            DraftQualityStatus.WARNING,
            "Claim 仍为 unverified，需在后续质量复核中核对陈述是否被证据充分支持。",
        ),
    ]
    if outline.unresolved_question_ids:
        checks.append(DraftQualityCheck(
            "research_gaps",
            DraftQualityStatus.WARNING,
            f"保留 {len(outline.unresolved_question_ids)} 个未解决研究问题，已在初稿中显式披露。",
        ))
    unused = tuple(record.evidence_id for record in evidence if record.evidence_id not in cited)
    if unused:
        checks.append(DraftQualityCheck(
            "unused_evidence",
            DraftQualityStatus.WARNING,
            f"有 {len(unused)} 条已登记 Evidence 未被本版 Claim 引用。",
        ))
    return tuple(checks)


def _render_markdown(
    outline: ReportOutline,
    claims: Sequence[ClaimRecord],
    evidence: Sequence[EvidenceRecord],
) -> str:
    evidence_by_id = {record.evidence_id: record for record in evidence}
    citation_labels: dict[str, str] = {}
    for claim in claims:
        for evidence_id in claim.evidence_ids:
            citation_labels.setdefault(evidence_id, f"E{len(citation_labels) + 1}")
    lines = [f"# {outline.report_title}", "", "> 状态：可审阅初稿；所有 Claim 尚待语义核验和人工评审。"]
    for section in outline.sections:
        lines.extend(("", f"## {section.title}", ""))
        section_claims = [claim for claim in claims if claim.section_id == section.section_id]
        for claim in section_claims:
            citations = "".join(f" [{citation_labels[evidence_id]}]" for evidence_id in claim.evidence_ids)
            lines.append(f"{claim.statement}{citations}")
            lines.append("")
        if section.unresolved_question_ids:
            lines.append("**待确认问题：** " + "、".join(section.unresolved_question_ids))
    lines.extend(("", "## 风险与不确定性", ""))
    if outline.unresolved_question_ids:
        lines.append("- 以下研究问题仍未解决：" + "、".join(outline.unresolved_question_ids))
    lines.append("- 本初稿中的 Claim 尚未完成语义核验或人工评审，不作为最终批准版本。")
    lines.extend(("", "## 来源与引用", ""))
    for evidence_id, label in citation_labels.items():
        record = evidence_by_id[evidence_id]
        lines.append(f"- [{label}] {record.locator}（evidence_id={record.evidence_id}）")
    return "\n".join(lines).strip()


def _evidence_id(source: SourceRecord, outline_version: int) -> str:
    identity = f"{source.work_id}:{outline_version}:{source.source_id}:{source.content_hash}"
    return f"evidence_sha256_{sha256(identity.encode()).hexdigest()}"


def _claim_id(
    work_id: str,
    outline_version: int,
    section_id: str,
    statement: str,
    claim_type: ClaimType,
    evidence_ids: Sequence[str],
) -> str:
    identity = json.dumps(
        [work_id, outline_version, section_id, statement, claim_type.value, list(evidence_ids)],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"claim_sha256_{sha256(identity.encode()).hexdigest()}"


def _metadata_texts(metadata: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = metadata.get(key)
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


def _section_evidence_ids(section: Mapping[str, object]) -> tuple[str, ...]:
    value = section.get("evidence_ids")
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _mapping_sequence(value: object, label: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        raise ValueError(f"report draft {label} must be a list of mappings")
    result: list[Mapping[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError(f"report draft {label} must be a list of mappings")
        result.append({str(key): nested for key, nested in item.items()})
    return tuple(result)


def _required_text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"report draft {key} must be non-blank text")
    return value


def _required_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"report draft {key} must be an integer")
    return value


def _text_tuple(payload: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"report draft {key} must be a list of text values")
    return tuple(value)


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{sha256(encoded).hexdigest()}"


def _digest_text(value: str) -> str:
    return f"sha256:{sha256(value.encode()).hexdigest()}"


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")


def _require_unique_nonblank(values: Sequence[str], field_name: str, *, allow_empty: bool = False) -> None:
    materialized = tuple(values)
    if not materialized and not allow_empty:
        raise ValueError(f"{field_name} must not be empty")
    if any(not value.strip() for value in materialized):
        raise ValueError(f"{field_name} must not contain blank values")
    _require_unique(materialized, field_name)


def _require_unique(values: Iterable[str], field_name: str) -> None:
    materialized = tuple(values)
    if len(materialized) != len(set(materialized)):
        raise ValueError(f"{field_name} must not contain duplicates")
