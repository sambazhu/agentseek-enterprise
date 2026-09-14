"""Fixed offline query controls; not a reproduction of production hybrid rank."""

from dataclasses import replace
from pathlib import Path

import pytest
from enterprise_wecom_digital_employee.evidence_relevance import relevant_content
from enterprise_wecom_digital_employee.report_brief import ResearchScope
from enterprise_wecom_digital_employee.report_research import (
    ReportResearchPlan,
    build_research_query,
    load_research_template,
)

TEMPLATE = Path(__file__).parents[1] / "digital_employees/industry-report/skills/report-intake/references/securities-industry-internal-research.yaml"


def plan() -> ReportResearchPlan:
    return ReportResearchPlan(
        work_id="fixture", report_brief_version=1,
        report_title="中国证券行业发展趋势与数字化转型研究报告",
        coverage_period="2025年至2026年上半年",
        research_scope=ResearchScope.SECURITIES_INDUSTRY,
        template=load_research_template(TEMPLATE),
    )


def test_six_queries_preserve_topic_and_question_concepts_without_shared_title() -> None:
    current = plan()
    questions = [q for s in current.template.sections for q in s.questions]
    queries = [build_research_query(current, q) for q in questions]
    assert len(set(queries)) == 6
    assert queries[0] == current.report_title
    for question, query in zip(questions[1:], queries[1:], strict=True):
        assert query == f"{question.prompt} 证券行业"
        assert current.report_title not in query
        assert current.coverage_period not in query
        assert question.top_k == 4
        assert question.minimum_fused_score == 0.02
    changed = replace(current, report_title="另一份证券行业研究", coverage_period="2024年")
    assert [build_research_query(changed, q) for q in questions[1:]] == queries[1:]
    assert build_research_query(changed, questions[0]) != queries[0]


@pytest.mark.parametrize("scope", [
    ResearchScope.SECURITIES_COMPANY, ResearchScope.SECURITIES_BUSINESS_LINE,
    ResearchScope.EXTERNAL_FACTOR_ON_SECURITIES,
])
def test_non_industry_scopes_keep_named_context(scope: ResearchScope) -> None:
    current = replace(plan(), research_scope=scope)
    question = current.template.sections[0].questions[1]
    assert build_research_query(current, question) == (
        f"报告主题：{current.report_title}；报告覆盖期：{current.coverage_period}；研究问题：{question.prompt}"
    )


def test_fixed_keyword_control_exposes_title_dominance_not_production_rank() -> None:
    current = plan()
    question = current.template.sections[2].questions[0]
    old = f"报告主题：{current.report_title}；报告覆盖期：{current.coverage_period}；研究问题：{question.prompt}"
    new = build_research_query(current, question)
    # A deliberately small exact-concept scorer isolates query composition.
    # It is not the MCP hybrid scorer and proves no live recall improvement.
    concepts = ("发展趋势", "数字化转型", "研究报告", "2025年", "2026年", "收入利润", "ROE", "资本效率")
    chunks = {
        **{f"stub-{i}": f"# {current.report_title}\n数据截至：{current.coverage_period}" for i in range(4)},
        "body": "证券公司的收入利润增长，ROE 为7.28%，资本效率提高。",
    }

    def ranked(query: str) -> list[str]:
        return sorted(chunks, key=lambda key: -sum(term in query and term in chunks[key] for term in concepts))[:4]

    assert "body" not in ranked(old)
    assert ranked(new)[0] == "body"
    assert relevant_content(chunks["body"], question.question_id, current.report_title)
    assert not relevant_content("IT部门收入利润增长，ROE 为7.28%。", question.question_id, current.report_title)


def test_structural_introduction_is_no_longer_fact_evidence() -> None:
    # Sentence-use follow-up explicitly closes the F5-known introduction gap.
    intro = "本报告从总量周期、政策沿革、分业务线六个维度展开，并给出面向券商投行条线的战略启示。"
    assert not relevant_content(intro, "business-line-benchmark.five-lines", plan().report_title)
