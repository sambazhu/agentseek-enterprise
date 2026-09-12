import pytest
from enterprise_wecom_digital_employee.evidence_relevance import is_evidence_sentence, relevant_content


@pytest.mark.parametrize("text", [
    "信息技术部制度：员工必须定期修改系统密码，提交采购审批。",
    "证券行业研究\n员工必须定期修改系统密码。IT 转型依赖采购审批。",
])
def test_it_policy_does_not_answer_securities_questions(text):
    for question in ("executive-summary.core-trends", "operating-benchmark.roe-and-structure", "action-recommendations.priorities"):
        assert not relevant_content(text, question, "证券行业数字化转型报告")


def test_it_document_can_support_digital_governance_but_not_financial_benchmark():
    text = "证券公司数字化转型须落实系统权限管理和治理责任。"
    assert relevant_content(text, "industry-overview.digital-transformation", "证券行业数字化转型报告")
    assert not relevant_content(text, "operating-benchmark.roe-and-structure", "证券行业数字化转型报告")
    assert not relevant_content(text, "business-line-benchmark.five-lines", "证券行业数字化转型报告")
    assert not relevant_content(text, "unknown-question", "证券行业数字化转型报告")


def test_factual_claim_must_preserve_whole_sentence_negation_and_numbers():
    evidence = ["证券公司收入没有增长。证券公司利润下降10%。"]
    assert is_evidence_sentence("证券公司利润下降10%。", evidence)
    for claim in ("证券公司收入增长。", "证券公司利润下降20%。", "利润下降10%", "证券公司利润增长10%。"):
        assert not is_evidence_sentence(claim, evidence)
