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


def test_direct_candidates_have_priority_over_background_within_same_top_four():
    question = "business-line-benchmark.five-lines"
    hits = {question: tuple(KnowledgeHit("doc", f"chunk-{i}", "title", 0.9, None, None) for i in range(4))}
    texts = ["券商经营业绩具有周期特征。", "券商财务杠杆为3倍。", "券商财富管理收入增长。", "券商自营投资收入增长。"]
    chunks = {f"chunk-{i}": {"content": value} for i, value in enumerate(texts)}
    plan = SimpleNamespace(report_title="证券行业报告", template=SimpleNamespace(sections=[
        SimpleNamespace(section_id="business", questions=[SimpleNamespace(question_id=question)]),
    ]))
    indexed, _, _ = _index_selected_hits(plan, hits, chunks)
    assert set(indexed) == {"chunk-2", "chunk-3"}


def test_background_source_persistence_does_not_close_coverage():
    from datetime import UTC, datetime
    from pathlib import Path

    from agentseek_work import WorkNotFoundError
    from enterprise_wecom_digital_employee.report_brief import ResearchScope
    from enterprise_wecom_digital_employee.report_research import (
        ReportResearchPlan,
        _coverage,
        _persist_sources,
        load_research_template,
    )

    class Repository:
        def get_source_record(self, **kwargs):
            raise WorkNotFoundError("not found")

        def put_source_record(self, record):
            return record

    template = load_research_template(Path(__file__).parents[1] / "digital_employees/industry-report/skills/report-intake/references/securities-industry-internal-research.yaml")
    plan = ReportResearchPlan("work-test", 1, "证券行业报告", "2026年", ResearchScope.SECURITIES_INDUSTRY, template)
    question = "business-line-benchmark.five-lines"
    hit = KnowledgeHit("doc", "chunk-1", "测试资料", 0.9, None, None)
    chunks = {"chunk-1": {"content": "券商财务杠杆为3倍。"}}
    questions, sections, indexed = _index_selected_hits(plan, {question: (hit,)}, chunks)
    sources = _persist_sources(
        composition=SimpleNamespace(repository=Repository()), work_id="work-test", tenant_id="tenant-test",
        contract_version=1, plan=plan, selected_chunk_ids=("chunk-1",), chunks_by_id=chunks,
        questions_by_chunk=questions, sections_by_chunk=sections, hit_by_chunk=indexed,
        query_by_question={question: "业务对标"}, clock=lambda: datetime.now(UTC),
    )
    assert len(sources) == 1
    assert sources[0].metadata["background_question_ids"] == [question]
    assert sources[0].metadata["evidence_use_version"] == "securities-evidence-use-v1"
    coverage = _coverage(plan, sources)
    assert coverage.covered_questions == 0
    assert question in coverage.gaps
