from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from agentseek_work import ClaimType
from enterprise_wecom_digital_employee.draft_generation import generate_draft_claims
from enterprise_wecom_digital_employee.report_draft import DraftContextResult
from enterprise_wecom_digital_employee.sentence_selection import (
    SentenceSelectionBatch,
    SentenceSelectionError,
    assemble_selections,
    sentence_choices,
)
from langchain_openai import ChatOpenAI

FACT = "证券公司投行业务收入增长12%。"


def context():
    evidence = SimpleNamespace(evidence_id="e1", source_id="s1", locator="mcp://test/doc#1",
        excerpt=FACT, confidence=0.9, metadata={"question_ids": ["business-line-benchmark.five-lines"], "section_ids": ["business"]})
    return DraftContextResult(work_id="test-work", report_outline_version=1, report_brief_version=1,
        report_title="证券行业报告", evidence=(evidence,), unavailable_source_ids=(),
        sections=({"section_id": "business", "title": "业务", "question_ids": ["business-line-benchmark.five-lines"], "evidence_ids": ["e1"]},))


class Model:
    def __init__(self, response=None):
        self.calls = []
        self.response = response

    def with_structured_output(self, schema, *, method):
        assert schema is SentenceSelectionBatch
        assert method == "function_calling"
        return self

    async def ainvoke(self, messages, config=None):
        self.calls.append((messages, config))
        candidate = json.loads(messages[1].content)["sentence_choices"][0]
        return self.response if self.response is not None else {"selections": [
            {"sentence_id": candidate["sentence_id"], "section_id": "business"},
        ]}


@pytest.mark.parametrize("invalid", [False, True])
def test_openai_compatible_wire_selects_ids_not_prose(invalid):
    requests = []
    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert "response_format" not in payload
        assert payload["tools"][0]["function"]["name"] == "SentenceSelectionBatch"
        candidates = json.loads(payload["messages"][1]["content"])["sentence_choices"]
        selection = {"sentence_id": candidates[0]["sentence_id"], "section_id": "business"}
        if invalid:
            selection["statement"] = "模型改写"
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 1, "model": "deepseek-chat",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{"id": "call_test", "type": "function", "function": {
                    "name": "SentenceSelectionBatch", "arguments": json.dumps({"selections": [selection]}),
                }}],
            }}],
        })
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            model = ChatOpenAI(model="deepseek-chat", api_key="test-only", base_url="https://provider.invalid/v1", http_async_client=client, max_retries=0)
            return await generate_draft_claims(context(), model=model)
    if invalid:
        with pytest.raises(RuntimeError, match="未保存任何 ReportDraft"):
            asyncio.run(run())
    else:
        proposal, = asyncio.run(run())
        assert proposal.statement == FACT
        assert proposal.claim_type == ClaimType.FACT
        assert proposal.evidence_ids == ["e1"]
    assert len(requests) == 1


def test_single_call_and_no_candidates_no_call():
    model = Model()
    result = asyncio.run(generate_draft_claims(context(), model=model, callbacks=(object(),)))
    assert result[0].statement == FACT
    assert len(model.calls) == 1
    assert len(model.calls[0][1]["callbacks"]) == 1
    assert asyncio.run(generate_draft_claims(replace(context(), evidence=()), model=model)) == ()
    assert len(model.calls) == 1


def test_selection_validates_all_errors_without_partial_results():
    choices = sentence_choices(context())
    good = {"sentence_id": choices[0]["sentence_id"], "section_id": "business"}
    batch = SentenceSelectionBatch.model_validate({"selections": [
        {"sentence_id": "fake", "section_id": "business"},
        {**good, "section_id": "other"}, good, good,
    ]})
    with pytest.raises(SentenceSelectionError) as failure:
        assemble_selections(batch, choices)
    assert failure.value.failures == [
        {"index": 0, "reason": "unknown_sentence_id"},
        {"index": 1, "reason": "section_not_allowed"},
        {"index": 3, "reason": "duplicate_selection"},
    ]
    assert FACT not in str(failure.value)


@pytest.mark.parametrize("change", ["work", "brief", "outline", "content"])
def test_stale_ids_rejected(change):
    current = context()
    old = sentence_choices(current)[0]["sentence_id"]
    if change == "content":
        current.evidence[0].excerpt = "证券公司投行业务收入增长13%。"
    else:
        key = {"work": "work_id", "brief": "report_brief_version", "outline": "report_outline_version"}[change]
        current = replace(current, **{key: "other" if change == "work" else 2})
    batch = SentenceSelectionBatch.model_validate({"selections": [{"sentence_id": old, "section_id": "business"}]})
    with pytest.raises(SentenceSelectionError):
        assemble_selections(batch, sentence_choices(current))


def test_noise_and_irrelevant_sentences_are_not_offered():
    current = context()
    current.evidence[0].excerpt = "本报告从证券投行两个维度展开。信息技术部需要完善数据安全策略。" + FACT
    assert [item["statement"] for item in sentence_choices(current)] == [FACT]


def test_repair_feedback_not_metadata_and_count_preserved():
    model = Model()
    feedback = {"failures": [{"index": 0, "reason": "unknown_sentence_id"}],
                "previous_selections": [{"sentence_id": "fake", "section_id": "business"}]}
    asyncio.run(generate_draft_claims(replace(context(), repair_feedback=feedback), model=model))
    messages, config = model.calls[0]
    assert json.loads(messages[1].content)["repair_feedback"] == feedback
    assert "only repair attempt" in messages[0].content
    assert config["metadata"]["repair_attempt"] == 1
    assert "fake" not in repr(config)
    with pytest.raises(SentenceSelectionError):
        assemble_selections(SentenceSelectionBatch(selections=[]), sentence_choices(context()), previous=feedback["previous_selections"])


def test_bounded_catalog_is_deterministic_and_keeps_other_evidence():
    current = context()
    first = current.evidence[0]
    first.excerpt = "".join(f"证券公司投行业务收入增长{i}%。" for i in range(150))
    second = SimpleNamespace(**{**vars(first), "evidence_id": "e2", "excerpt": FACT})
    current = replace(current, evidence=(first, second), sections=({**current.sections[0], "evidence_ids": ["e1", "e2"]},))
    choices = sentence_choices(current)
    assert len(choices) == 120
    assert choices == sentence_choices(current)
    assert choices[1]["evidence_id"] == "e2"


def test_background_and_company_case_choices_keep_usage():
    current = context()
    current.evidence[0].excerpt = "本公司为适应证券市场数字化转型趋势，完善公司信息技术治理。"
    current.evidence[0].metadata["question_ids"] = ["executive-summary.core-trends"]
    current = replace(current, sections=({**current.sections[0], "question_ids": ["executive-summary.core-trends"]},))
    assert sentence_choices(current)[0]["section_uses"] == {"business": "company_case"}
    current.evidence[0].excerpt = "证券公司讨论财富管理。这种转型推动自营配置变化。"
    current.evidence[0].metadata["question_ids"] = ["business-line-benchmark.five-lines"]
    current = replace(current, sections=({**current.sections[0], "question_ids": ["business-line-benchmark.five-lines"]},))
    assert sentence_choices(current)[1]["section_uses"] == {"business": "background"}
