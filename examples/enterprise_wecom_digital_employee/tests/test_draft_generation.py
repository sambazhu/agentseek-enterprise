from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import cast

import httpx
import pytest
from agentseek_work import ClaimType
from enterprise_wecom_digital_employee.draft_generation import (
    DraftClaimBatch,
    generate_draft_claims,
)
from enterprise_wecom_digital_employee.report_draft import DraftContextResult
from langchain_openai import ChatOpenAI


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

    def with_structured_output(self, schema: object, *, method: str) -> _StructuredRunnable:
        assert schema is DraftClaimBatch
        assert method == "function_calling"
        return self.runnable


@pytest.mark.parametrize("invalid", [False, True])
def test_openai_compatible_wire_uses_tool_calling_and_validates_output(invalid: bool) -> None:
    requests = []
    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        assert "response_format" not in payload
        assert payload["tools"][0]["function"]["name"] == "DraftClaimBatch"
        claim = {"section_id": "summary", "statement": "需要更多证据", "claim_type": "risk", "evidence_ids": []}
        arguments = {"claims": [claim]} if not invalid else {"claims": [], "unexpected": "bad"}
        return httpx.Response(200, json={
            "id": "test-completion", "object": "chat.completion", "created": 1, "model": "deepseek-chat",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{"id": "call_test", "type": "function", "function": {
                    "name": "DraftClaimBatch", "arguments": json.dumps(arguments),
                }}],
            }}],
        })
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            model = ChatOpenAI(model="deepseek-chat", api_key="test-only", base_url="https://provider.invalid/v1", http_async_client=client, max_retries=0)
            context = DraftContextResult(
                work_id="test-work", report_outline_version=1, report_brief_version=1, report_title="测试报告",
                evidence=(), unavailable_source_ids=(), sections=({"section_id": "summary", "title": "摘要", "evidence_ids": ["test-evidence"]},),
            )
            return await generate_draft_claims(context, model=model)
    if invalid:
        with pytest.raises(RuntimeError, match="未保存任何 ReportDraft"):
            asyncio.run(run())
    else:
        assert len(asyncio.run(run())) == 1
    assert len(requests) == 1


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
            "evidence_ids": ["test-evidence"],
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


def test_evidence_free_draft_skips_model_entirely() -> None:
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

    assert runnable.calls == []
    assert claims == ()
