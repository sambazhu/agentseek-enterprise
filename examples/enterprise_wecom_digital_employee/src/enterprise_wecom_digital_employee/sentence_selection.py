"""Request-local, evidence-bound sentence choices. No model-authored quotations."""

import json
from hashlib import sha256

from agentseek_work import ClaimType
from pydantic import BaseModel, ConfigDict, Field

from enterprise_wecom_digital_employee.evidence_relevance import (
    _sentences,
    content_use,
    is_evidence_sentence,
)
from enterprise_wecom_digital_employee.report_draft import (
    _FORBIDDEN_CONTENT_RE,
    MAX_DRAFT_CLAIMS,
    DraftClaimProposal,
    DraftContextResult,
)

SELECTION_VERSION = "evidence-sentence-selection-v1"
MAX_SENTENCE_CHOICES = 120


class SentenceSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    sentence_id: str = Field(min_length=1, max_length=80)
    section_id: str = Field(min_length=1, max_length=256)


class SentenceSelectionBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    selections: list[SentenceSelection] = Field(max_length=MAX_DRAFT_CLAIMS)


class SentenceSelectionError(ValueError):
    def __init__(self, selections: list[dict[str, str]], failures: list[dict[str, object]]) -> None:
        super().__init__("证据句选择未通过完整校验，本轮未保存初稿。")
        self.selections = selections
        self.failures = failures


def sentence_choices(context: DraftContextResult) -> list[dict[str, object]]:  # noqa: C901 - bounded evidence/section projection
    choices = []
    seen = set()
    for evidence in context.evidence:
        for sentence in _sentences(evidence.excerpt):
            if len(sentence) > 1600 or _FORBIDDEN_CONTENT_RE.search(sentence):
                continue
            if not is_evidence_sentence(sentence, [evidence.excerpt]):
                continue
            uses = {}
            question_ids = set(evidence.metadata.get("question_ids", []))
            for section in context.sections:
                if evidence.evidence_id not in section.get("evidence_ids", []):
                    continue
                kinds = [content_use(evidence.excerpt, key, context.report_title, statement=sentence)
                         for key in section.get("question_ids", []) if key in question_ids]
                use = next((kind for kind in ("direct", "company_case", "background") if kind in kinds), None)
                if use:
                    uses[str(section["section_id"])] = use
            if not uses:
                continue
            identity = [SELECTION_VERSION, context.work_id, context.report_brief_version,
                        context.report_outline_version, evidence.evidence_id, evidence.excerpt, sentence, uses]
            sentence_id = "sentence_" + sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            if sentence_id in seen:
                continue
            seen.add(sentence_id)
            choices.append({"sentence_id": sentence_id, "statement": sentence,
                            "evidence_id": evidence.evidence_id, "section_uses": uses})
    # Round-robin across evidence records avoids one long excerpt taking all slots.
    groups = {}
    for choice in choices:
        groups.setdefault(choice["evidence_id"], []).append(choice)
    bounded = []
    for index in range(max((len(group) for group in groups.values()), default=0)):
        for group in groups.values():
            if index < len(group):
                bounded.append(group[index])
            if len(bounded) == MAX_SENTENCE_CHOICES:
                return bounded
    return bounded


def assemble_selections(batch: SentenceSelectionBatch, choices: list[dict[str, object]],
                        previous: list[dict[str, str]] | None = None) -> tuple[DraftClaimProposal, ...]:
    by_id = {choice["sentence_id"]: choice for choice in choices}
    failures = []
    proposals = []
    seen = set()
    if previous is not None and len(batch.selections) != len(previous):
        failures.append({"index": None, "reason": "selection_count_changed"})
    for index, selection in enumerate(batch.selections):
        choice = by_id.get(selection.sentence_id)
        if choice is None:
            failures.append({"index": index, "reason": "unknown_sentence_id"})
            continue
        if selection.section_id not in choice["section_uses"]:
            failures.append({"index": index, "reason": "section_not_allowed"})
            continue
        key = (selection.section_id, choice["statement"], choice["evidence_id"])
        if key in seen:
            failures.append({"index": index, "reason": "duplicate_selection"})
        seen.add(key)
        proposals.append(DraftClaimProposal(section_id=selection.section_id, statement=choice["statement"],
                                            claim_type=ClaimType.FACT, evidence_ids=[choice["evidence_id"]]))
    if failures:
        raise SentenceSelectionError([selection.model_dump() for selection in batch.selections], failures)
    return tuple(proposals)
