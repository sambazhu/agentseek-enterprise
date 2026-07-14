from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Any

import yaml
from agentseek_work import (
    ExcerptStatus,
    SnapshotStatus,
    SourceRecord,
    SourceType,
    WorkContractStatus,
    WorkNotFoundError,
)

from {{ cookiecutter.project_slug }}.report_brief import (
    REPORT_BRIEF_CONTRACT_TYPE,
    ReportBrief,
)
from {{ cookiecutter.project_slug }}.work_composition import (
    IndustryReportWorkComposition,
    WorkCompositionError,
)

MCPInvoker = Callable[[str, str, dict[str, Any], bool], Awaitable[str]]


class CoverageStatus(StrEnum):
    COVERED = "covered"
    PARTIAL = "partial"
    GAP = "gap"


@dataclass(frozen=True, slots=True)
class ResearchQuestion:
    question_id: str
    prompt: str
    search_mode: str
    top_k: int
    minimum_fused_score: float
    minimum_keyword_score: float
    minimum_semantic_score: float

    def __post_init__(self) -> None:
        _require_text(self.question_id, "question_id")
        _require_text(self.prompt, "prompt")
        if self.search_mode not in {"keyword", "semantic", "hybrid"}:
            raise ValueError("search_mode is unsupported")
        if not 1 <= self.top_k <= 20:
            raise ValueError("top_k must be between 1 and 20")
        for field_name in (
            "minimum_fused_score",
            "minimum_keyword_score",
            "minimum_semantic_score",
        ):
            if not 0.0 <= getattr(self, field_name) <= 1.0:
                raise ValueError(f"{field_name} must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class ResearchSection:
    section_id: str
    title: str
    questions: tuple[ResearchQuestion, ...]

    def __post_init__(self) -> None:
        _require_text(self.section_id, "section_id")
        _require_text(self.title, "title")
        if not self.questions:
            raise ValueError("research section requires at least one question")
        _require_unique((item.question_id for item in self.questions), "question_id")


@dataclass(frozen=True, slots=True)
class ReportResearchTemplate:
    template_id: str
    template_version: str
    report_asset_ref: str
    sections: tuple[ResearchSection, ...]

    def __post_init__(self) -> None:
        for field_name in ("template_id", "template_version", "report_asset_ref"):
            _require_text(getattr(self, field_name), field_name)
        if not self.sections:
            raise ValueError("research template requires sections")
        _require_unique((item.section_id for item in self.sections), "section_id")

    @property
    def digest(self) -> str:
        return _digest(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "template_version": self.template_version,
            "report_asset_ref": self.report_asset_ref,
            "sections": [
                {
                    "section_id": section.section_id,
                    "title": section.title,
                    "questions": [
                        {
                            "question_id": question.question_id,
                            "prompt": question.prompt,
                            "search_mode": question.search_mode,
                            "top_k": question.top_k,
                            "minimum_fused_score": question.minimum_fused_score,
                            "minimum_keyword_score": question.minimum_keyword_score,
                            "minimum_semantic_score": question.minimum_semantic_score,
                        }
                        for question in section.questions
                    ],
                }
                for section in self.sections
            ],
        }


@dataclass(frozen=True, slots=True)
class ReportResearchPlan:
    work_id: str
    report_brief_version: int
    report_title: str
    coverage_period: str
    template: ReportResearchTemplate

    @property
    def digest(self) -> str:
        return _digest({
            "work_id": self.work_id,
            "report_brief_version": self.report_brief_version,
            "report_title": self.report_title,
            "coverage_period": self.coverage_period,
            "template_digest": self.template.digest,
        })


@dataclass(frozen=True, slots=True)
class KnowledgeHit:
    document_id: str
    chunk_id: str
    title: str
    score: float
    keyword_score: float | None
    semantic_score: float | None


@dataclass(frozen=True, slots=True)
class QuestionCoverage:
    question_id: str
    status: CoverageStatus
    source_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SectionCoverage:
    section_id: str
    title: str
    status: CoverageStatus
    questions: tuple[QuestionCoverage, ...]


@dataclass(frozen=True, slots=True)
class ResearchCoverage:
    covered_questions: int
    total_questions: int
    ratio: float
    sections: tuple[SectionCoverage, ...]

    @property
    def gaps(self) -> tuple[str, ...]:
        return tuple(
            question.question_id
            for section in self.sections
            for question in section.questions
            if question.status is CoverageStatus.GAP
        )


@dataclass(frozen=True, slots=True)
class InternalResearchResult:
    plan: ReportResearchPlan
    sources: tuple[SourceRecord, ...]
    coverage: ResearchCoverage

    def as_dict(self) -> dict[str, Any]:
        return {
            "work_id": self.plan.work_id,
            "report_brief_version": self.plan.report_brief_version,
            "template_id": self.plan.template.template_id,
            "template_version": self.plan.template.template_version,
            "research_plan_digest": self.plan.digest,
            "source_ids": [source.source_id for source in self.sources],
            "coverage": {
                "covered_questions": self.coverage.covered_questions,
                "total_questions": self.coverage.total_questions,
                "ratio": self.coverage.ratio,
                "gaps": list(self.coverage.gaps),
                "sections": [
                    {
                        "section_id": section.section_id,
                        "title": section.title,
                        "status": section.status.value,
                        "questions": [
                            {
                                "question_id": question.question_id,
                                "status": question.status.value,
                                "source_ids": list(question.source_ids),
                            }
                            for question in section.questions
                        ],
                    }
                    for section in self.coverage.sections
                ],
            },
            "external_search_used": False,
        }


def load_research_template(path: Path) -> ReportResearchTemplate:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    root = _mapping(loaded, "research template")
    if root.get("schema_version") != 1:
        raise ValueError("unsupported research template schema_version")
    sections: list[ResearchSection] = []
    for raw_section in _sequence(root.get("sections"), "sections"):
        section = _mapping(raw_section, "section")
        questions: list[ResearchQuestion] = []
        for raw_question in _sequence(section.get("questions"), "questions"):
            question = _mapping(raw_question, "question")
            questions.append(ResearchQuestion(
                question_id=_text(question, "question_id"),
                prompt=_text(question, "prompt"),
                search_mode=_text(question, "search_mode"),
                top_k=int(question.get("top_k", 4)),
                minimum_fused_score=float(question.get("minimum_fused_score", 0.02)),
                minimum_keyword_score=float(question.get("minimum_keyword_score", 0.08)),
                minimum_semantic_score=float(question.get("minimum_semantic_score", 0.7)),
            ))
        sections.append(ResearchSection(
            section_id=_text(section, "section_id"),
            title=_text(section, "title"),
            questions=tuple(questions),
        ))
    return ReportResearchTemplate(
        template_id=_text(root, "template_id"),
        template_version=_text(root, "template_version"),
        report_asset_ref=_text(root, "report_asset_ref"),
        sections=tuple(sections),
    )


def build_research_plan(contract: Any, template: ReportResearchTemplate) -> ReportResearchPlan:
    if contract.contract_type != REPORT_BRIEF_CONTRACT_TYPE:
        raise ValueError("research requires a report-brief contract")
    if contract.status is not WorkContractStatus.CONFIRMED:
        raise ValueError("formal research requires a confirmed ReportBrief")
    brief = ReportBrief.from_contract(contract)
    return ReportResearchPlan(
        work_id=contract.work_id,
        report_brief_version=contract.contract_version,
        report_title=brief.title,
        coverage_period=brief.coverage_period,
        template=template,
    )


async def run_internal_research(
    *,
    composition: IndustryReportWorkComposition,
    state: Mapping[str, object],
    runtime_context: object | None,
    template_path: Path,
    invoke_mcp: MCPInvoker,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> InternalResearchResult:
    item = composition.current_work(state, runtime_context)
    if item is None:
        raise WorkCompositionError("当前员工没有可执行内部研究的进行中报告任务。")
    contract = composition.repository.get_current_work_contract(
        tenant_id=item.tenant_id,
        work_id=item.work_id,
        contract_type=REPORT_BRIEF_CONTRACT_TYPE,
    )
    if contract is None or contract.status is not WorkContractStatus.CONFIRMED:
        raise WorkCompositionError("请先形成并确认 ReportBrief，再启动内部知识检索。")
    plan = build_research_plan(contract, load_research_template(template_path))

    hits_by_question, query_by_question = await _search_plan(plan, invoke_mcp)
    selected_chunk_ids, chunks_by_id = await _read_selected_chunks(hits_by_question, invoke_mcp)
    questions_by_chunk, sections_by_chunk, hit_by_chunk = _index_selected_hits(
        plan,
        hits_by_question,
        chunks_by_id,
    )
    sources = _persist_sources(
        composition=composition,
        work_id=item.work_id,
        tenant_id=item.tenant_id,
        contract_version=contract.contract_version,
        plan=plan,
        selected_chunk_ids=selected_chunk_ids,
        chunks_by_id=chunks_by_id,
        questions_by_chunk=questions_by_chunk,
        sections_by_chunk=sections_by_chunk,
        hit_by_chunk=hit_by_chunk,
        query_by_question=query_by_question,
        clock=clock,
    )

    coverage = _coverage(plan, sources)
    return InternalResearchResult(plan=plan, sources=tuple(sources), coverage=coverage)


async def _search_plan(
    plan: ReportResearchPlan,
    invoke_mcp: MCPInvoker,
) -> tuple[dict[str, tuple[KnowledgeHit, ...]], dict[str, str]]:
    hits_by_question: dict[str, tuple[KnowledgeHit, ...]] = {}
    query_by_question: dict[str, str] = {}
    for section in plan.template.sections:
        for question in section.questions:
            query = (
                f"报告主题：{plan.report_title}；报告覆盖期：{plan.coverage_period}；"
                f"研究问题：{question.prompt}"
            )
            query_by_question[question.question_id] = query
            raw = await invoke_mcp(
                "department-knowledge",
                "knowledge_search",
                {"query": query, "search_mode": question.search_mode, "top_k": question.top_k},
                False,
            )
            hits_by_question[question.question_id] = _parse_hits(raw, question=question)
    return hits_by_question, query_by_question


async def _read_selected_chunks(
    hits_by_question: Mapping[str, Sequence[KnowledgeHit]],
    invoke_mcp: MCPInvoker,
) -> tuple[tuple[str, ...], dict[str, Mapping[str, Any]]]:
    selected = tuple(dict.fromkeys(hit.chunk_id for hits in hits_by_question.values() for hit in hits[:2]))
    if not selected:
        return (), {}
    raw = await invoke_mcp(
        "department-knowledge",
        "knowledge_read_chunks",
        {"chunk_ids": list(selected)},
        False,
    )
    return selected, _parse_chunks(raw)


def _index_selected_hits(
    plan: ReportResearchPlan,
    hits_by_question: Mapping[str, Sequence[KnowledgeHit]],
    chunks_by_id: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, KnowledgeHit]]:
    questions_by_chunk: dict[str, set[str]] = {}
    sections_by_chunk: dict[str, set[str]] = {}
    hit_by_chunk: dict[str, KnowledgeHit] = {}
    for section in plan.template.sections:
        for question in section.questions:
            for hit in hits_by_question[question.question_id][:2]:
                if hit.chunk_id not in chunks_by_id:
                    continue
                questions_by_chunk.setdefault(hit.chunk_id, set()).add(question.question_id)
                sections_by_chunk.setdefault(hit.chunk_id, set()).add(section.section_id)
                previous = hit_by_chunk.get(hit.chunk_id)
                if previous is None or hit.score > previous.score:
                    hit_by_chunk[hit.chunk_id] = hit
    return questions_by_chunk, sections_by_chunk, hit_by_chunk


def _persist_sources(
    *,
    composition: IndustryReportWorkComposition,
    work_id: str,
    tenant_id: str,
    contract_version: int,
    plan: ReportResearchPlan,
    selected_chunk_ids: Sequence[str],
    chunks_by_id: Mapping[str, Mapping[str, Any]],
    questions_by_chunk: Mapping[str, set[str]],
    sections_by_chunk: Mapping[str, set[str]],
    hit_by_chunk: Mapping[str, KnowledgeHit],
    query_by_question: Mapping[str, str],
    clock: Callable[[], datetime],
) -> list[SourceRecord]:
    sources: list[SourceRecord] = []
    for chunk_id in selected_chunk_ids:
        chunk = chunks_by_id.get(chunk_id)
        hit = hit_by_chunk.get(chunk_id)
        if chunk is None or hit is None:
            continue
        content_hash = _digest_text(_text(chunk, "content"))
        identity = f"{work_id}:{contract_version}:{hit.document_id}:{chunk_id}:{content_hash}"
        source_id = f"source_sha256_{sha256(identity.encode()).hexdigest()}"
        try:
            source = composition.repository.get_source_record(tenant_id=tenant_id, source_id=source_id)
        except WorkNotFoundError:
            locator = f"mcp://department-knowledge/{hit.document_id}#{chunk_id}"
            query_digests = sorted(
                _digest_text(query_by_question[question_id]) for question_id in questions_by_chunk[chunk_id]
            )
            source = composition.repository.put_source_record(SourceRecord(
                source_id=source_id,
                work_id=work_id,
                tenant_id=tenant_id,
                source_type=SourceType.DEPARTMENT_KNOWLEDGE,
                title=hit.title,
                publisher="战略发展部",
                retrieved_at=clock(),
                locator=locator,
                uri_digest=_digest_text(locator),
                content_hash=content_hash,
                result_digest=_digest({
                    "document_id": hit.document_id,
                    "chunk_id": chunk_id,
                    "score": hit.score,
                    "keyword_score": hit.keyword_score,
                    "semantic_score": hit.semantic_score,
                    "content_hash": content_hash,
                }),
                confidentiality_level="internal",
                authority_level="approved_internal",
                allowed_uses=("research", "citation"),
                snapshot_policy="reference_only",
                snapshot_status=SnapshotStatus.REFERENCED,
                retrieval_query_digest=_digest(query_digests),
                excerpt_status=ExcerptStatus.NOT_REQUESTED,
                license_terms_ref="internal-policy://department-knowledge/v1",
                metadata={
                    "provider": "department-knowledge",
                    "document_id": hit.document_id,
                    "chunk_id": chunk_id,
                    "section_ids": sorted(sections_by_chunk[chunk_id]),
                    "question_ids": sorted(questions_by_chunk[chunk_id]),
                    "research_plan_digest": plan.digest,
                    "report_brief_version": contract_version,
                },
            ))
        sources.append(source)
    return sources


def _coverage(plan: ReportResearchPlan, sources: Sequence[SourceRecord]) -> ResearchCoverage:
    source_ids_by_question: dict[str, list[str]] = {}
    for source in sources:
        question_ids = source.metadata.get("question_ids")
        if not isinstance(question_ids, list):
            continue
        for question_id in question_ids:
            source_ids_by_question.setdefault(str(question_id), []).append(source.source_id)
    sections: list[SectionCoverage] = []
    covered = 0
    total = 0
    for section in plan.template.sections:
        questions: list[QuestionCoverage] = []
        for question in section.questions:
            total += 1
            source_ids = tuple(dict.fromkeys(source_ids_by_question.get(question.question_id, [])))
            status = CoverageStatus.COVERED if source_ids else CoverageStatus.GAP
            covered += int(bool(source_ids))
            questions.append(QuestionCoverage(question.question_id, status, source_ids))
        statuses = {question.status for question in questions}
        section_status = (
            CoverageStatus.COVERED
            if statuses == {CoverageStatus.COVERED}
            else CoverageStatus.GAP
            if statuses == {CoverageStatus.GAP}
            else CoverageStatus.PARTIAL
        )
        sections.append(SectionCoverage(section.section_id, section.title, section_status, tuple(questions)))
    return ResearchCoverage(
        covered_questions=covered,
        total_questions=total,
        ratio=round(covered / total, 4) if total else 0.0,
        sections=tuple(sections),
    )


def _parse_hits(raw: str, *, question: ResearchQuestion) -> tuple[KnowledgeHit, ...]:
    payload = _json_mapping(raw, "knowledge_search")
    hits: list[KnowledgeHit] = []
    for raw_hit in _sequence(payload.get("hits", []), "hits"):
        hit = _mapping(raw_hit, "hit")
        score = float(hit.get("score", 0.0))
        keyword_score = _optional_score(hit, "keyword_score")
        semantic_score = _optional_score(hit, "semantic_score")
        if not _passes_relevance_gate(
            question,
            score=score,
            keyword_score=keyword_score,
            semantic_score=semantic_score,
        ):
            continue
        hits.append(KnowledgeHit(
            document_id=_text(hit, "document_id"),
            chunk_id=_text(hit, "chunk_id"),
            title=_text(hit, "title"),
            score=score,
            keyword_score=keyword_score,
            semantic_score=semantic_score,
        ))
    return tuple(hits)


def _passes_relevance_gate(
    question: ResearchQuestion,
    *,
    score: float,
    keyword_score: float | None,
    semantic_score: float | None,
) -> bool:
    if question.search_mode == "keyword":
        return (keyword_score if keyword_score is not None else score) >= question.minimum_keyword_score
    if question.search_mode == "semantic":
        return (semantic_score if semantic_score is not None else score) >= question.minimum_semantic_score
    return any((
        score >= question.minimum_fused_score,
        keyword_score is not None and keyword_score >= question.minimum_keyword_score,
        semantic_score is not None and semantic_score >= question.minimum_semantic_score,
    ))


def _optional_score(value: Mapping[str, Any], field_name: str) -> float | None:
    raw = value.get(field_name)
    return float(raw) if raw is not None else None


def _parse_chunks(raw: str) -> dict[str, Mapping[str, Any]]:
    payload = _json_mapping(raw, "knowledge_read_chunks")
    chunks: dict[str, Mapping[str, Any]] = {}
    for raw_chunk in _sequence(payload.get("chunks", []), "chunks"):
        chunk = _mapping(raw_chunk, "chunk")
        chunks[_text(chunk, "chunk_id")] = chunk
    return chunks


def _json_mapping(raw: str, label: str) -> Mapping[str, Any]:
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} returned non-JSON content") from exc
    return _mapping(loaded, label)


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    return {str(key): item for key, item in value.items()}


def _sequence(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{label} must be a sequence")
    return value


def _text(value: Mapping[str, Any], field_name: str) -> str:
    result = str(value.get(field_name, "")).strip()
    _require_text(result, field_name)
    return result


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")


def _require_unique(values: Iterable[str], field_name: str) -> None:
    materialized = tuple(values)
    if len(materialized) != len(set(materialized)):
        raise ValueError(f"{field_name} must not contain duplicates")


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{sha256(encoded).hexdigest()}"


def _digest_text(value: str) -> str:
    return f"sha256:{sha256(value.encode()).hexdigest()}"
