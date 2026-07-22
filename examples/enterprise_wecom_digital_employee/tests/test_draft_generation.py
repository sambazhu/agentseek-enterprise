from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import cast

from agentseek_work import ClaimType
from enterprise_wecom_digital_employee.draft_generation import (
    DraftClaimBatch,
    generate_draft_claims,
)
from enterprise_wecom_digital_employee.report_draft import DraftContextResult


class _StructuredRunnable:
    def __init__(self) -> None:
        self.calls: list[tuple[object, object]] = []

    async def ainvoke(self, value: object, config: object = None) -> object:
        self.calls.append((value, config))
        return {
            "claims": [{
                "section_id": "executive-summary",
                "statement": "本节仍有问题需要补充证据后确认。",
                "claim_type": "risk",
                "evidence_ids": [],
            }],
        }


class _StructuredModel:
    def __init__(self, runnable: _StructuredRunnable) -> None:
        self.runnable = runnable

    def with_structured_output(self, schema: object) -> _StructuredRunnable:
        assert schema is DraftClaimBatch
        return self.runnable


def test_claim_generation_forces_one_structured_model_call() -> None:
    runnable = _StructuredRunnable()
    context = DraftContextResult(
        work_id="work_test",
        report_outline_version=2,
        report_brief_version=3,
        report_title="证券行业报告",
        evidence=(),
        unavailable_source_ids=(),
        sections=({
            "section_id": "executive-summary",
            "title": "执行摘要",
            "question_ids": ["q1"],
            "unresolved_question_ids": ["q1"],
            "evidence_ids": [],
        },),
    )

    claims = asyncio.run(generate_draft_claims(
        context,
        model=_StructuredModel(runnable),
        callbacks=(object(),),
    ))

    assert len(runnable.calls) == 1
    assert claims[0].claim_type is ClaimType.RISK
    assert claims[0].section_id == "executive-summary"
    assert claims[0].evidence_ids == []
    _messages, raw_config = runnable.calls[0]
    config = cast(Mapping[str, object], raw_config)
    metadata = cast(Mapping[str, object], config["metadata"])
    callbacks = cast(list[object], config["callbacks"])
    assert metadata["work_id"] == "work_test"
    assert len(callbacks) == 1


def test_claim_generation_replaces_cross_section_citation_for_evidence_free_gap() -> None:
    class CrossSectionRunnable(_StructuredRunnable):
        async def ainvoke(self, value: object, config: object = None) -> object:
            self.calls.append((value, config))
            return {
                "claims": [{
                    "section_id": "executive-summary",
                    "statement": "模型错误地用其他章节证据补齐了缺口。",
                    "claim_type": "fact",
                    "evidence_ids": ["evidence_other_section"],
                }],
            }

    runnable = CrossSectionRunnable()
    context = DraftContextResult(
        work_id="work_test",
        report_outline_version=2,
        report_brief_version=3,
        report_title="证券行业报告",
        evidence=(),
        unavailable_source_ids=(),
        sections=({
            "section_id": "executive-summary",
            "title": "执行摘要",
            "question_ids": ["q1"],
            "unresolved_question_ids": ["q1"],
            "evidence_ids": [],
        },),
    )

    claims = asyncio.run(generate_draft_claims(
        context,
        model=_StructuredModel(runnable),
    ))

    assert len(runnable.calls) == 1
    assert len(claims) == 1
    assert claims[0].section_id == "executive-summary"
    assert claims[0].claim_type is ClaimType.RISK
    assert claims[0].evidence_ids == []
    assert claims[0].statement == "执行摘要仍存在未解决问题，相关判断需补充适用证据后确认。"
