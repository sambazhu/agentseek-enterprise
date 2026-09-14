import asyncio
import json
from types import SimpleNamespace

import pytest
from enterprise_wecom_digital_employee.evidence_relevance import relevant_content
from enterprise_wecom_digital_employee.report_research import KnowledgeHit, _index_selected_hits, _read_selected_chunks


@pytest.mark.parametrize("text", [
    "# 证券行业战略转型\n**券商/投行 内部战略参考**\n---\n来源：内部资料",
    "券商/投行 内部战略参考", "证券行业战略转型", "**证券行业数字化转型**",
    "# 证券利润增长10%\n作者：研究组\n数据截至：2026-09-14",
])
def test_heading_metadata_stubs_do_not_qualify(text):
    for question in ("executive-summary.core-trends", "business-line-benchmark.five-lines", "industry-overview.digital-transformation"):
        assert not relevant_content(text, question, "证券行业发展趋势报告")


@pytest.mark.parametrize("text,question", [
    ("券商利润下降。", "operating-benchmark.roe-and-structure"),
    ("**证券公司利润增长10%。**", "executive-summary.core-trends"),
    ("# 证券行业\n证券公司利润下降10%。", "executive-summary.core-trends"),
    ("| 证券公司 | ROE | 7.28% |", "operating-benchmark.roe-and-structure"),
    ("证券公司投行业务收入为100亿元", "business-line-benchmark.five-lines"),
])
def test_short_facts_body_after_heading_and_numeric_rows_survive(text, question):
    assert relevant_content(text, question, "证券行业发展趋势报告")


@pytest.mark.parametrize("only_noise", [False, True])
def test_top_four_checks_body_beyond_two_stubs_and_caps_two_admissions(only_noise):
    question = "executive-summary.core-trends"
    hits = {question: tuple(KnowledgeHit("doc", f"chunk-{i}", "title", 1.0-i/10, None, None) for i in range(5))}
    content = ["# 证券行业转型", "来源：证券行业增长参考", "证券公司利润增长10%。", "证券公司利润下降5%。", "证券公司利润增长20%。"]
    if only_noise:
        content[2:4] = ["信息技术部制度要求采购审批。"] * 2
    calls = []
    async def invoke(server, tool, args, confirmed):
        calls.append((server, tool, args, confirmed))
        return json.dumps({"chunks": [{"chunk_id": f"chunk-{i}", "content": content[i]} for i in range(4)]})
    selected, chunks = asyncio.run(_read_selected_chunks(hits, invoke))
    assert selected == ("chunk-0", "chunk-1", "chunk-2", "chunk-3")
    assert len(calls) == 1
    plan = SimpleNamespace(report_title="证券行业发展趋势报告", sections=None,
        template=SimpleNamespace(sections=[SimpleNamespace(section_id="summary", questions=[SimpleNamespace(question_id=question)])]))
    indexed, _, _ = _index_selected_hits(plan, hits, chunks)
    assert set(indexed) == (set() if only_noise else {"chunk-2", "chunk-3"})
